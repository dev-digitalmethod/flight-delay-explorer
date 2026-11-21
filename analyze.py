"""Analyze a monthly BTS reporting-carrier flight archive for RDU and CLT."""

import argparse
import csv
import hashlib
import io
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter


AIRPORTS = ("RDU", "CLT")
ARCHIVE_STEM = "On_Time_Reporting_Carrier_On_Time_Performance_1987_present"
DOWNLOAD_ROOT = "https://www.transtats.bts.gov/PREZIP"
BENCHMARK_MONTH = "2025-10"
BENCHMARK = {
    ("RDU", "arrival"): (4932, 77.7),
    ("CLT", "arrival"): (16266, 85.5),
    ("RDU", "departure"): (4929, 80.4),
    ("CLT", "departure"): (16273, 80.4),
}
FLIGHT_KEY = (
    "FlightDate", "DOT_ID_Reporting_Airline", "Flight_Number_Reporting_Airline",
    "OriginAirportID", "DestAirportID", "CRSDepTime",
)
MEASURES = (
    ("arrival", "Dest", "ArrDelay", "ArrDel15"),
    ("departure", "Origin", "DepDelay", "DepDel15"),
)
REQUIRED = set(FLIGHT_KEY) | {"Cancelled", "Diverted", "TaxiOut"} | {
    column for _, *columns in MEASURES for column in columns
}


def percentile(values, p):
    """Linearly interpolate between adjacent sorted observations."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (rank - lower) * (ordered[upper] - ordered[lower])


def read_archive(path):
    buckets = {(a, m[0]): Counter() for a in AIRPORTS for m in MEASURES}
    delays = {key: [] for key in buckets}
    taxi = {airport: [] for airport in AIRPORTS}
    quality = Counter()
    seen = set()
    month = None
    with ZipFile(path) as archive:
        member, = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        with archive.open(member) as stream:
            reader = csv.reader(io.TextIOWrapper(stream, encoding="utf-8-sig"))
            header = next(reader)
            missing = REQUIRED - set(header)
            if missing:
                raise ValueError(f"Missing columns: {', '.join(sorted(missing))}")
            index = {name: header.index(name) for name in REQUIRED}
            for row in reader:
                quality["national_flight_records"] += 1
                flight_month = row[index["FlightDate"]][:7]
                if month is None:
                    month = flight_month
                if flight_month != month:
                    raise ValueError("Expected a single flight month")
                if not any(row[index[column]] in AIRPORTS for _, column, *_ in MEASURES):
                    continue

                quality["flight_records_touching_rdu_or_clt"] += 1
                key = tuple(row[index[column]] for column in FLIGHT_KEY)
                if key in seen:
                    quality["duplicate_key_candidates"] += 1
                seen.add(key)
                flags = tuple(float(row[index[column]]) for column in ("Cancelled", "Diverted"))
                if any(flag not in (0, 1) for flag in flags) or sum(flags) > 1:
                    raise ValueError(f"Invalid flight status at CSV line {reader.line_num}")
                cancelled, diverted = map(bool, flags)
                for direction, airport_column, delay_column, indicator in MEASURES:
                    airport = row[index[airport_column]]
                    if airport not in AIRPORTS:
                        continue
                    group = (airport, direction)
                    counts = buckets[group]
                    counts["reported"] += 1
                    counts["cancelled"] += cancelled
                    counts["diverted"] += diverted
                    text = row[index[delay_column]]
                    delay = float(text) if text else None
                    has_delay = delay is not None
                    if not cancelled and not diverted:
                        counts["completed_with_delay"] += has_delay
                        counts["completed_missing_delay"] += not has_delay
                        if has_delay:
                            delays[group].append(delay)
                    eligible = not cancelled and (direction == "departure" or not diverted)
                    if eligible and has_delay:
                        counts["on_time"] += delay < 15
                        counts["delay_flag_disagreements"] += (
                            row[index[indicator]] == ""
                            or float(row[index[indicator]]) != int(delay >= 15)
                        )
                    if direction == "departure" and not cancelled:
                        value = row[index["TaxiOut"]]
                        if value:
                            taxi[airport].append(float(value))
    if not month or any(not counts["reported"] for counts in buckets.values()):
        raise ValueError("No data for the required month and airport groups")
    quality.setdefault("duplicate_key_candidates", 0)
    return month, member, buckets, delays, taxi, quality


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def summarize(buckets, delays, taxi):
    results = []
    for (airport, direction), counts in sorted(buckets.items()):
        sample = delays[airport, direction]
        result = dict(
            airport=airport, direction=direction, reported=counts["reported"],
            on_time=counts["on_time"],
            on_time_pct=round(100 * counts["on_time"] / counts["reported"], 1),
            cancelled=counts["cancelled"], diverted=counts["diverted"],
            completed_with_delay=counts["completed_with_delay"],
            completed_missing_delay=counts["completed_missing_delay"],
            delay_flag_disagreements=counts["delay_flag_disagreements"],
            median_delay_min=percentile(sample, 0.50), p90_delay_min=percentile(sample, 0.90),
            p95_delay_min=percentile(sample, 0.95),
        )
        outgoing_taxi = taxi[airport] if direction == "departure" else []
        result.update(
            taxi_out_n=len(outgoing_taxi) if direction == "departure" else None,
            median_taxi_out_min=percentile(outgoing_taxi, 0.50),
            p95_taxi_out_min=percentile(outgoing_taxi, 0.95),
        )
        results.append(result)
    return results


def plot_arrivals(delays, month, output):
    fig, ax = plt.subplots(figsize=(8.5, 4.35), layout="constrained")
    ax.set_axisbelow(True)
    ax.grid(axis="y", color="#e7e8f2")
    for airport, color in zip(AIRPORTS, ("#4546a6", "#277580")):
        counts = Counter(delays[airport, "arrival"])
        total = counts.total()
        if not total:
            continue
        observations = sorted(counts)
        cumulative = 0
        shares = []
        for value in observations:
            cumulative += counts[value]
            shares.append(cumulative / total)
        ax.step(
            [min(observations[0] - 1, -45), *observations, max(observations[-1] + 1, 120)],
            [0, *shares, 1], where="post", color=color, linewidth=2.1,
            label=f"{airport} ({total:,} arrivals)",
        )
    ax.axvline(15, color="#61677b", linestyle="--", lw=1.0, label="15-minute late threshold")
    ax.set(xlim=(-45, 120), ylim=(0, 1.02), xlabel="Gate arrival delay (minutes)",
           ylabel="Share of arrivals at or below delay", title=f"Arrival delay distribution · {month}")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="lower right", frameon=False)
    fig.savefig(output, dpi=165)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument("archive", nargs="?", type=Path, default=root / "data/bts-2025-10.zip")
    parser.add_argument("--output", type=Path, default=root / "results")
    args = parser.parse_args()
    path = args.archive
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    month, member, buckets, delays, taxi, quality = read_archive(path)
    year, month_number = month.split("-")
    source = f"{DOWNLOAD_ROOT}/{ARCHIVE_STEM}_{year}_{int(month_number)}.zip"
    results = summarize(buckets, delays, taxi)
    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "airport_metrics.csv", results)
    write_csv(args.output / "data_quality.csv", [
        {"check": name, "value": value} for name, value in quality.items()
    ])
    benchmark_rows = []
    if month == BENCHMARK_MONTH:
        for row in results:
            key = row["airport"], row["direction"]
            published_count, published_rate = BENCHMARK[key]
            benchmark_rows.append({
                "airport": key[0], "direction": key[1], "computed_count": row["reported"],
                "dot_count": published_count, "computed_on_time_pct": row["on_time_pct"],
                "dot_on_time_pct": published_rate,
                "match": (row["reported"], row["on_time_pct"]) == BENCHMARK[key],
            })
        write_csv(args.output / "dot_table5_reconciliation.csv", benchmark_rows)
    else:
        (args.output / "dot_table5_reconciliation.csv").unlink(missing_ok=True)
    if not all(item["match"] for item in benchmark_rows):
        raise SystemExit("October 2025 results differ from DOT Table 5. Check reconciliation CSV.")
    plot_arrivals(delays, month, args.output / "arrival_delay_distribution.png")
    (args.output / "provenance.txt").write_text(
        f"Flight month: {month}\nBTS monthly archive URL: {source}\n"
        f"Local file: {path.name}\nCSV member: {member}\n"
        f"ZIP bytes: {path.stat().st_size}\nSHA-256: {digest}\n",
        encoding="utf-8",
    )
    print(f"{month}: {quality['national_flight_records']:,} records scanned.")
    if benchmark_rows:
        print(f"DOT Table 5: {len(benchmark_rows) * 2}/8 counts and percentages match.")
    print(f"Outputs: {args.output}")


if __name__ == "__main__":
    main()
