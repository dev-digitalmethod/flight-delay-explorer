"""Export exact monthly departure-delay frequency tables for the public Explorer."""

import argparse
from collections import defaultdict
from datetime import date, timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import duckdb

from signal_analysis import available_partitions, next_month

PROJECT = Path(__file__).resolve().parent
SOURCE = ("https://www.transtats.bts.gov/TableInfo.asp"
          "?QO_fu146_anzr=b0-gvzr&gnoyr_VQ=FGJ")
QUERY = """
WITH flights AS (
    SELECT Origin AS airport, Reporting_Airline AS carrier,
           CASE WHEN Cancelled THEN 'cancelled' WHEN Diverted THEN 'diverted'
                WHEN NOT Cancelled AND NOT Diverted THEN 'completed' ELSE 'unknown' END AS outcome,
           CASE WHEN isfinite(DepDelay) THEN DepDelay END AS delay
    FROM read_parquet(?, hive_partitioning = false)
)
SELECT CASE WHEN grouping(airport) = 1 THEN '*' ELSE airport END AS scope,
       carrier, outcome, delay, count(*) AS flights
FROM flights
GROUP BY GROUPING SETS ((airport, carrier, outcome, delay), (carrier, outcome, delay))
ORDER BY scope, carrier, outcome, delay NULLS FIRST
"""


def write_json(path, value):
    encoded = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    path.write_bytes(encoded + b"\n")


def export(root, output):
    partitions, _ = available_partitions(root, root / ".no-sample")
    if not partitions:
        raise ValueError(f"No monthly Parquet partitions found under {root / 'flights'}.")

    # Each entry stores one month's carrier and outcome counts for one airport.
    # * contains everyone, including records with no origin or carrier code.
    records = defaultdict(list)
    carriers = set()
    totals = defaultdict(int)
    with TemporaryDirectory(prefix="bts-export-") as scratch:
        with duckdb.connect(config={"threads": "2", "memory_limit": "1GB",
                                    "temp_directory": scratch}) as con:
            for month, path in partitions.items():
                groups = {}
                result = con.execute(QUERY, [path])
                while batch := result.fetchmany(16384):
                    for airport, carrier, outcome, delay, flights in batch:
                        if not airport:
                            continue
                        if carrier:
                            carriers.add(carrier)
                        key = (airport, carrier or "", outcome)
                        entry = groups.get(key)
                        if entry is None:
                            entry = [month, key[1], outcome, 0, []]
                            groups[key] = entry
                            records[airport].append(entry)
                        totals[airport] += flights
                        if delay is None:
                            entry[3] += flights
                        else:
                            value = int(delay) if delay.is_integer() else delay
                            entry[4].extend((value, flights))
                print(f"Exported {month}", flush=True)

    output.mkdir(parents=True, exist_ok=True)
    chunks = {}
    for airport, rows in records.items():
        slug = "all" if airport == "*" else airport.lower()
        if airport != "*" and not re.fullmatch(r"[A-Z0-9]{3}", airport):
            raise ValueError(f"Unexpected BTS airport code {airport!r}.")
        encoded = json.dumps(rows, separators=(",", ":"), allow_nan=False).encode()
        digest = sha256(encoded).hexdigest()[:16]
        name = f"{slug}-{digest}.json.gz"
        target = output / name
        if not target.exists():
            with NamedOutput(output) as temporary:
                temporary.write_bytes(gzip.compress(encoded, compresslevel=6, mtime=0))
                temporary.replace(target)
        chunks[airport] = name

    months = sorted(partitions)
    last = date.fromisoformat(months[-1] + "-01")
    airports = sorted(chunks.keys() - {"*"})
    manifest = {
        "months": months,
        "lastDate": (next_month(last) - timedelta(days=1)).isoformat(),
        "source": SOURCE,
        "values": {"airline": sorted(carriers), "origin": airports},
        "topOrigins": sorted(airports, key=lambda airport: (-totals[airport], airport))[:3],
        "chunks": chunks,
    }
    with NamedOutput(output) as temporary:
        write_json(temporary, manifest)
        temporary.replace(output / "manifest.json")

    generated_name = re.compile(r"(?:all|[a-z0-9]{3})-[0-9a-f]{16}\.json\.gz")
    for path in output.glob("*.json.gz"):
        if generated_name.fullmatch(path.name) and path.name not in chunks.values():
            path.unlink()
    size = sum((output / name).stat().st_size for name in chunks.values())
    print(f"{len(months)} months, {len(airports)} airports, {size / 2**20:.1f} MiB.")


class NamedOutput:
    """Use a local temporary file so the final replacement is atomic."""

    def __init__(self, directory):
        self.scratch = TemporaryDirectory(dir=directory)

    def __enter__(self):
        return Path(self.scratch.name) / "payload"

    def __exit__(self, *_):
        self.scratch.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=PROJECT / "data/bts")
    parser.add_argument("--output", type=Path, default=PROJECT / "docs/data")
    args = parser.parse_args()
    export(args.data_root, args.output)


if __name__ == "__main__":
    main()
