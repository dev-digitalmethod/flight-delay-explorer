"""Discover BTS operating-carrier archives and build monthly Parquet partitions."""

import csv
import hashlib
import io
import json
import re
import shutil
import tempfile
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

import duckdb

INDEX_URL = "https://www.transtats.bts.gov/PREZIP/"
ARCHIVE_NAME = re.compile(
    r"On_Time_Reporting_Carrier_On_Time_Performance_1987_present_"
    r"([0-9]{4})_(0?[1-9]|1[0-2])\.zip", re.IGNORECASE
)
DEFAULT_START, DEFAULT_END = "2022-01", "2025-12"
SCHEMA_VERSION = 1

# Keep HHMM fields as source strings so leading zeroes and 2400 survive.
TEXT_COLUMNS = """
Reporting_Airline IATA_CODE_Reporting_Airline Tail_Number Flight_Number_Reporting_Airline
Origin OriginCityName OriginState OriginStateFips OriginStateName
Dest DestCityName DestState DestStateFips DestStateName
CRSDepTime DepTime DepTimeBlk WheelsOff WheelsOn CRSArrTime ArrTime ArrTimeBlk
CancellationCode FirstDepTime
""".split()
INTEGER_COLUMNS = """
Year Quarter Month DayofMonth DayOfWeek DOT_ID_Reporting_Airline
OriginAirportID OriginAirportSeqID OriginCityMarketID OriginWac
DestAirportID DestAirportSeqID DestCityMarketID DestWac
DepartureDelayGroups ArrivalDelayGroups DistanceGroup DivAirportLandings
""".split()
NUMERIC_COLUMNS = """
DepDelay DepDelayMinutes TaxiOut TaxiIn ArrDelay ArrDelayMinutes CRSElapsedTime
ActualElapsedTime AirTime Flights Distance CarrierDelay WeatherDelay NASDelay SecurityDelay
LateAircraftDelay TotalAddGTime LongestAddGTime DivActualElapsedTime DivArrDelay DivDistance
""".split()
FLAG_COLUMNS = ("DepDel15", "ArrDel15", "Cancelled", "Diverted", "DivReachedDest")
for stop in range(1, 6):
    prefix = f"Div{stop}"
    TEXT_COLUMNS.extend(prefix + field for field in ("Airport", "WheelsOn", "WheelsOff", "TailNum"))
    INTEGER_COLUMNS.extend(prefix + field for field in ("AirportID", "AirportSeqID"))
    NUMERIC_COLUMNS.extend(prefix + field for field in ("TotalGTime", "LongestGTime"))

COLUMN_TYPES = {
    "FlightDate": "DATE",
    **dict.fromkeys(TEXT_COLUMNS, "VARCHAR"),
    **dict.fromkeys(INTEGER_COLUMNS, "INTEGER"),
    **dict.fromkeys(NUMERIC_COLUMNS, "DOUBLE"),
    **dict.fromkeys(FLAG_COLUMNS, "BOOLEAN"),
}


class CatalogParser(HTMLParser):
    def __init__(self, index_url):
        super().__init__()
        self.index_url = index_url
        self.archives = {}

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        url = urljoin(self.index_url, href)
        parts = urlsplit(url)
        if not is_bts_url(url) or not parts.path.lower().startswith("/prezip/"):
            return
        match = ARCHIVE_NAME.fullmatch(unquote(parts.path.rsplit("/", 1)[-1]))
        if match:
            year, month = match.groups()
            self.archives[f"{year}-{int(month):02d}"] = url


def is_bts_url(url):
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc in {
        "www.transtats.bts.gov", "transtats.bts.gov",
    }


def open_bts(url):
    if not is_bts_url(url):
        raise ValueError("Expected an HTTPS URL on BTS TranStats.")
    request = Request(url, headers={"User-Agent": "DigitalMethod-BTS-Downloader/1.0"})
    response = urlopen(request, timeout=45)
    if not is_bts_url(response.geturl()):
        response.close()
        raise ValueError("BTS returned a redirect outside TranStats.")
    return response


def fetch_catalog():
    with open_bts(INDEX_URL) as response:
        parser = CatalogParser(response.geturl())
        parser.feed(response.read().decode(response.headers.get_content_charset() or "utf-8",
                                           errors="replace"))
    if not parser.archives:
        raise ValueError("No monthly BTS reporting-carrier archives were found in the listing.")
    return dict(sorted(parser.archives.items()))


def selected_months(catalog, start, end):
    if start not in catalog or end not in catalog:
        raise ValueError("Both months must be available in the BTS catalog.")
    if end < start:
        raise ValueError("The end month must be on or after the start month.")
    current = date.fromisoformat(start + "-01")
    months = []
    while current.isoformat()[:7] <= end:
        months.append(current.isoformat()[:7])
        if months[-1] == end:
            break
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    missing = [month for month in months if month not in catalog]
    if missing:
        raise ValueError("BTS listing is missing these months: " + ", ".join(missing))
    return months


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download_archive(url, path, progress):
    staged = path.with_name("download.zip")
    for attempt in range(1, 4):
        try:
            with open_bts(url) as response, staged.open("wb") as stream:
                length = response.headers.get("Content-Length")
                expected = int(length) if length else None
                received = 0
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
                    received += len(chunk)
                    sizes = f"{received / 1048576:.1f} MiB"
                    if expected:
                        sizes += f" / {expected / 1048576:.1f} MiB"
                    progress(f"Downloading {sizes}")
                if expected is not None and received != expected:
                    raise OSError(f"BTS download incomplete: {received:,} of {expected:,} bytes")
            with ZipFile(staged) as archive:
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise BadZipFile(f"CRC check failed for {bad_member}")
            path.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(path)
            return
        except (OSError, HTTPException, BadZipFile) as error:
            if attempt == 3 or (isinstance(error, HTTPError) and error.code in (403, 404)):
                raise
            progress(f"Download interrupted. Retrying ({attempt + 1}/3).")
            time.sleep(attempt)


def unpack_csv(source, target):
    with ZipFile(source) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"Expected one BTS CSV in {source.name}; found {len(members)}.")
        with archive.open(members[0]) as stream, target.open("wb") as output:
            shutil.copyfileobj(stream, output, 1024 * 1024)
    with target.open("r", encoding="utf-8-sig", newline="") as stream:
        header = next(csv.reader(stream), [])
    if header and not header[-1]:
        header[-1] = "_bts_trailing_field"
    expected = set(COLUMN_TYPES)
    unknown = set(header) - expected - {"_bts_trailing_field"}
    missing = expected - set(header)
    if missing or unknown or len(header) != len(set(header)):
        raise ValueError(
            f"Unexpected BTS CSV header in {source.name}. Missing: {sorted(missing)}. "
            f"Unknown: {sorted(unknown)}. Check for duplicate headers."
        )
    return members[0], header


def column_expression(name, dtype):
    # All names here come from the fixed schema, never from arbitrary CSV input.
    field = f'"{name}"'
    if dtype == "VARCHAR":
        return field
    if dtype == "BOOLEAN":
        return (
            f"CASE WHEN {field} IS NULL THEN NULL "
            f"WHEN CAST({field} AS DOUBLE) = 0 THEN FALSE "
            f"WHEN CAST({field} AS DOUBLE) = 1 THEN TRUE "
            f"ELSE error('Non-binary value in {name}') END AS {field}"
        )
    return f"CAST({field} AS {dtype}) AS {field}"


def parquet_from_csv(csv_path, header, output, month, scratch):
    year, month_number = map(int, month.split("-"))
    columns = ", ".join(column_expression(name, dtype) for name, dtype in COLUMN_TYPES.items())
    csv_columns = dict.fromkeys(header, "VARCHAR")
    with duckdb.connect(config={"memory_limit": "1GB", "threads": "2",
                               "temp_directory": str(scratch)}) as con:
        # DuckDB binds both the source and destination paths as parameters.
        statement = f"""
            COPY (SELECT {columns} FROM read_csv($source, columns = $schema, auto_detect = false,
                  header = true, delim = ',', quote = '"', escape = '"', strict_mode = true))
            TO $output (FORMAT PARQUET, COMPRESSION ZSTD)
        """
        copied, = con.execute(statement, {
            "source": str(csv_path), "schema": csv_columns, "output": str(output),
        }).fetchone()
        count, first_day, last_day, invalid = con.execute("""
            SELECT count(*), min(FlightDate), max(FlightDate),
                count(*) FILTER (WHERE FlightDate IS NULL OR year(FlightDate) <> ?
                    OR month(FlightDate) <> ? OR "Year" IS DISTINCT FROM ?
                    OR "Month" IS DISTINCT FROM ?
                    OR Cancelled IS NULL OR Diverted IS NULL OR (Cancelled AND Diverted))
            FROM read_parquet(?)
        """, [year, month_number, year, month_number, str(output)]).fetchone()
        if not count or copied != count or invalid:
            raise ValueError(f"Invalid {month} output. Wrote {copied:,}; read {count:,}; "
                             f"{invalid:,} rows have invalid dates or flight status.")
    return count, first_day, last_day


def read_manifest(path):
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        return entry if isinstance(entry, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def ingest_month(root, month, url, progress):
    raw = root / "raw" / Path(urlsplit(url).path).name
    year, month_number = month.split("-")
    parquet = root / "flights" / f"flight_year={year}" / f"flight_month={month_number}" / "data.parquet"
    manifest = root / "manifest" / f"{month}.json"
    previous = read_manifest(manifest)
    if (previous.get("schema_version") == SCHEMA_VERSION and previous.get("source_url") == url
            and raw.is_file() and parquet.is_file()
            and sha256(raw) == previous.get("archive_sha256")
            and sha256(parquet) == previous.get("parquet_sha256")):
        progress("Already downloaded and verified")
        return previous

    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root, prefix=".staging-") as folder:
        stage = Path(folder)
        if not raw.is_file():
            download_archive(url, stage / raw.name, progress)
            raw.parent.mkdir(parents=True, exist_ok=True)
            (stage / raw.name).replace(raw)
        csv_path = stage / "read.csv"
        progress("Reading BTS archive")
        try:
            member, header = unpack_csv(raw, csv_path)
        except BadZipFile:
            progress("Cached ZIP is corrupt. Downloading a fresh copy.")
            download_archive(url, stage / raw.name, progress)
            (stage / raw.name).replace(raw)
            member, header = unpack_csv(raw, csv_path)
        progress("Converting and validating Parquet")
        staged_parquet = stage / "data.parquet"
        count, first_day, last_day = parquet_from_csv(
            csv_path, header, staged_parquet, month, stage / "duckdb"
        )
        entry = {
            "schema_version": SCHEMA_VERSION,
            "flight_month": month,
            "source_url": url,
            "archive_path": raw.relative_to(root).as_posix(),
            "archive_member": member,
            "archive_bytes": raw.stat().st_size,
            "archive_sha256": sha256(raw),
            "parquet_path": parquet.relative_to(root).as_posix(),
            "parquet_bytes": staged_parquet.stat().st_size,
            "parquet_sha256": sha256(staged_parquet),
            "rows": count,
            "first_flight_date": first_day.isoformat(),
            "last_flight_date": last_day.isoformat(),
            "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        staged_manifest = stage / "manifest.json"
        staged_manifest.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
        parquet.parent.mkdir(parents=True, exist_ok=True)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        staged_parquet.replace(parquet)
        staged_manifest.replace(manifest)
    return entry
