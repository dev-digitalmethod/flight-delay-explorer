"""Download or explore local BTS flight data."""

import argparse
import json
import os
import secrets
import threading
import webbrowser
from contextlib import contextmanager
from functools import lru_cache
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb

from bts_data import (
    DEFAULT_END, DEFAULT_START, INDEX_URL, fetch_catalog, ingest_month, selected_months,
)
from signal_analysis import available_partitions, evaluate, metadata

PROJECT = Path(__file__).resolve().parent
SAMPLE = PROJECT / "demo_data/october-2025.parquet"


@lru_cache(maxsize=1)
def plotly_script():
    from plotly.offline import get_plotlyjs

    return get_plotlyjs().encode("utf-8")


@contextmanager
def lock_output(root):
    """Use an OS lock so it is released even if the downloader is interrupted."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".download.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError(f"Another download is using {root}.") from error
        yield


def write_months(root, months, catalog, update):
    total_rows = 0
    benchmark = None
    for index, month in enumerate(months, start=1):
        def progress(message):
            update(f"{month} · {message}", index - 1, total_rows, benchmark)

        progress("Starting")
        entry = ingest_month(root, month, catalog[month], progress)
        if month == "2025-10":
            from verify_parquet import reconcile

            progress("Checking against DOT Table 5")
            benchmark = reconcile(
                root / entry["parquet_path"], root / "checks/dot_table5_parquet.csv"
            )
        total_rows += entry["rows"]
        update(f"{month} · {entry['rows']:,} records ready", index, total_rows, benchmark)


def acquire(root, months, catalog, update):
    with lock_output(root):
        write_months(root, months, catalog, update)


class DownloadJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"state": "idle"}

    def status(self):
        with self.lock:
            return dict(self.state)

    def start(self, root, months, catalog):
        with self.lock:
            if self.state["state"] == "running":
                raise ValueError("A download is already running.")
            self.state = {
                "state": "running", "total": len(months), "completed": 0, "rows": 0,
                "message": "Starting download", "benchmark": None,
            }
        threading.Thread(target=self.run, args=(root, months, catalog), daemon=True).start()

    def run(self, root, months, catalog):
        def update(message, completed, rows, benchmark):
            with self.lock:
                self.state.update(message=message, completed=completed, rows=rows,
                                  benchmark=benchmark)

        try:
            acquire(root, months, catalog, update)
        except Exception as error:
            with self.lock:
                self.state.update(state="error", message=str(error))
        else:
            with self.lock:
                self.state.update(state="done", message="Parquet dataset ready")


def serve(root, port, launch_browser, explore=False):
    page = (PROJECT / "picker.html").read_bytes()
    explorer = (PROJECT / "explorer.html").read_bytes()
    job = DownloadJob()
    token = secrets.token_urlsafe(24)
    catalog_lock = threading.Lock()
    catalog = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send_content(self, body, mime, status=200):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def send_json(self, data, status=200):
            self.send_content(json.dumps(data, allow_nan=False).encode(), "application/json", status)

        def host_allowed(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            nonlocal catalog
            if not self.host_allowed():
                self.send_json({"error": "Open the loopback URL shown in the terminal."}, 403)
            elif self.path == "/":
                self.send_content(page, "text/html; charset=utf-8")
            elif self.path == "/explore":
                self.send_content(explorer, "text/html; charset=utf-8")
            elif self.path == "/assets/plotly.js":
                self.send_content(plotly_script(), "application/javascript; charset=utf-8")
            elif self.path == "/api/explore":
                try:
                    dataset = metadata(*available_partitions(root, SAMPLE))
                    self.send_json({**dataset, "token": token})
                except (OSError, duckdb.Error, ValueError) as error:
                    self.send_json({"error": f"Could not read the Parquet dataset. {error}"}, 500)
            elif self.path == "/api/catalog":
                try:
                    with catalog_lock:
                        catalog = fetch_catalog()
                        months = list(catalog)
                except (OSError, HTTPException, ValueError) as error:
                    self.send_json({"error": f"Could not load BTS catalog. {error}"}, 502)
                    return
                self.send_json({
                    "months": months, "defaultStart": DEFAULT_START, "defaultEnd": DEFAULT_END,
                    "dataDirectory": str(root), "token": token,
                })
            elif self.path == "/api/status":
                self.send_json(job.status())
            else:
                self.send_json({"error": "Page not found."}, 404)

        def do_POST(self):
            if not self.host_allowed() or self.headers.get("X-Download-Token") != token:
                self.send_json({"error": "Reload the local month picker."}, 403)
                return
            if self.path not in {"/api/download", "/api/evaluate"}:
                self.send_json({"error": "Page not found."}, 404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get_content_type() != "application/json" or not 0 < length <= 8192:
                    raise ValueError("Expected a JSON request.")
                body = json.loads(self.rfile.read(length))
                if self.path == "/api/evaluate":
                    partitions, _ = available_partitions(root, SAMPLE)
                    self.send_json(evaluate(body, partitions))
                    return
                if not isinstance(body, dict) or not all(
                    isinstance(body.get(name), str) for name in ("start", "end")
                ):
                    raise ValueError("Supply both a start and an end month.")
                with catalog_lock:
                    months = selected_months(catalog, body["start"], body["end"])
                    job.start(root, months, catalog)
            except (ValueError, TypeError) as error:
                self.send_json({"error": str(error)}, 400)
                return
            except (OSError, duckdb.Error) as error:
                self.send_json({"error": f"Could not evaluate the Parquet dataset. {error}"}, 500)
                return
            self.send_json(job.status(), 202)

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        if explore:
            url += "/explore"
        print(f"Open {url} in your browser. Press Ctrl+C to stop.", flush=True)
        if launch_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--data-dir", default=PROJECT / "data/bts", type=Path,
                     help="Directory for raw ZIPs, Parquet, manifests and checks")
    cli.add_argument("--list", action="store_true", help="Print available months from BTS")
    cli.add_argument("--start", metavar="YYYY-MM")
    cli.add_argument("--end", metavar="YYYY-MM")
    cli.add_argument("--port", type=int, default=0, help="Local picker port")
    cli.add_argument("--no-browser", action="store_true", help="Print URL without opening browser")
    cli.add_argument("--explore", action="store_true", help="Open the signal explorer")
    args = cli.parse_args()
    root = args.data_dir.expanduser().resolve()

    if bool(args.start) != bool(args.end) or (args.list and args.start) or (
        args.explore and (args.list or args.start)
    ):
        cli.error("Choose --explore, --list, or both --start and --end.")
    try:
        if args.list or args.start:
            catalog = fetch_catalog()
            if args.list:
                print("\n".join(catalog))
                return
            months = selected_months(catalog, args.start, args.end)

            def print_progress(message, completed, rows, benchmark):
                if "Downloading " not in message:
                    print(f"[{completed}/{len(months)}] {message}", flush=True)
                if benchmark and completed == len(months):
                    print(benchmark)

            acquire(root, months, catalog, print_progress)
            print(f"Data directory: {root}")
        else:
            serve(root, args.port, not args.no_browser, args.explore)
    except Exception as error:
        cli.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
