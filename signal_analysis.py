"""Query the same filtered flight records for trends, statistics and distributions."""

from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from math import isfinite
from tempfile import TemporaryDirectory

import duckdb

SIGNALS = {
    "ArrDelay": ("Arrival delay", "min"),
    "DepDelay": ("Departure delay", "min"),
    "TaxiOut": ("Taxi-out time", "min"),
    "TaxiIn": ("Taxi-in time", "min"),
    "AirTime": ("Airborne time", "min"),
    "CRSElapsedTime": ("Scheduled gate-to-gate time", "min"),
    "ActualElapsedTime": ("Actual gate-to-gate time", "min"),
    "Distance": ("Route distance", "mi"),
    "CarrierDelay": ("Reported carrier delay", "min"),
    "WeatherDelay": ("Reported weather delay", "min"),
    "NASDelay": ("Reported national air system delay", "min"),
    "SecurityDelay": ("Reported security delay", "min"),
    "LateAircraftDelay": ("Reported late aircraft delay", "min"),
}
GROUPS = {
    "airline": ("Reporting_Airline", "Operating airline"),
    "origin": ("Origin", "Origin airport"),
    "destination": ("Dest", "Destination airport"),
}
GRAINS = {"day": "1 day", "week": "1 week", "month": "1 month"}
AGGREGATES = {
    "mean": "avg(selected_value)",
    "median": "median(selected_value)",
    "p95": "quantile_cont(selected_value, 0.95)",
    "count": "count(selected_value)",
}
OUTCOMES = {
    "all": "TRUE",
    "completed": "NOT Cancelled AND NOT Diverted",
    "cancelled": "Cancelled",
    "diverted": "Diverted",
}
SUMMARY_KEYS = (
    "n", "missing", "outside", "mean", "median", "p95", "stddev", "min", "max",
    "p01", "p99",
)
MAX_GROUPS = 5


def available_partitions(root, sample):
    partitions = {}
    for path in sorted(root.glob("flights/flight_year=*/flight_month=*/data.parquet")):
        month = path.parent.name.split("=", 1)[1]
        year = path.parent.parent.name.split("=", 1)[1]
        try:
            key = date(int(year), int(month), 1).isoformat()[:7]
        except ValueError:
            continue
        partitions[key] = str(path)
    if partitions:
        return partitions, "downloaded"
    if sample.is_file():
        return {"2025-10": str(sample)}, "sample"
    return {}, "none"


def next_month(day):
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def metadata(partitions, source):
    months = sorted(partitions)
    last = date.fromisoformat(months[-1] + "-01") if months else None
    values = {name: [] for name in GROUPS}
    if months:
        columns = ", ".join(
            f"list(DISTINCT {column} ORDER BY {column})"
            for column, _ in GROUPS.values()
        )
        with duckdb.connect(config={"threads": "2"}) as con:
            row = con.execute(
                f"SELECT {columns} FROM read_parquet(?, hive_partitioning = false)",
                [list(partitions.values())],
            ).fetchone()
        values = {name: [code for code in codes or [] if code]
                  for name, codes in zip(GROUPS, row)}
    return {
        "source": source, "months": months,
        "lastDate": (next_month(last) - timedelta(days=1)).isoformat() if last else None,
        "signals": [{"id": key, "label": label, "unit": unit}
                    for key, (label, unit) in SIGNALS.items()],
        "values": values,
    }


def optional_number(value):
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ValueError("Use finite numbers for signal limits.")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError("Use finite numbers for signal limits.") from error
    if not isfinite(number):
        raise ValueError("Use finite numbers for signal limits.")
    return number


def selection(request, partitions):
    if not isinstance(request, dict):
        raise ValueError("Choose a period and signal.")
    try:
        start = date.fromisoformat(request["start"])
        end = date.fromisoformat(request["end"])
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("Choose valid start and end dates.") from error
    if start > end:
        raise ValueError("The end date must be on or after the start date.")
    if not partitions:
        raise ValueError("Download a BTS month to begin.")
    if start.isoformat()[:7] < min(partitions) or end.isoformat()[:7] > max(partitions):
        raise ValueError("Choose dates within the available BTS months.")
    month = start.replace(day=1)
    files, gaps = [], []
    while True:
        key = month.isoformat()[:7]
        if key in partitions:
            files.append(partitions[key])
        else:
            gaps.append(key)
        if key == end.isoformat()[:7]:
            break
        month = next_month(month)
    if not files:
        raise ValueError("No Parquet files cover the selected period.")
    return start, end, files, gaps


def calendar(start, end, grain):
    if grain == "week":
        start -= timedelta(days=start.weekday())
    if grain == "month":
        start = start.replace(day=1)
    result = []
    while start <= end:
        result.append(start.isoformat())
        if grain == "month":
            start = next_month(start)
        else:
            start += timedelta(days=7 if grain == "week" else 1)
    return result


def load_filtered(con, request, files, start, end, signal, group, grain, outcome):
    key = GROUPS[group][0] if group else "NULL::VARCHAR"
    constraints = ["FlightDate BETWEEN ? AND ?", OUTCOMES[outcome]]
    params = [files, start, end]
    filters = request.get("filters", {})
    if not isinstance(filters, dict):
        raise ValueError("Invalid flight filters.")
    for name, (column, _) in GROUPS.items():
        code = filters.get(name, "")
        if not isinstance(code, str) or len(code) > 12:
            raise ValueError("Invalid airline or airport filter.")
        if code:
            constraints.append(f"{column} = ?")
            params.append(code)
    con.execute(f"""
        CREATE TEMP TABLE filtered AS
        SELECT time_bucket(INTERVAL '{GRAINS[grain]}', FlightDate) AS bucket,
               {key} AS group_code,
               CASE WHEN isfinite("{signal}") THEN "{signal}" END AS value
        FROM read_parquet(?, hive_partitioning = false)
        WHERE {" AND ".join(constraints)}
    """, params)


def select_series(con, group, groups, total, limits):
    lower, upper = limits
    if group and not groups:
        rows = con.execute("""
            SELECT group_code
            FROM filtered
            WHERE group_code IS NOT NULL AND value IS NOT NULL
                AND (? IS NULL OR value >= ?) AND (? IS NULL OR value <= ?)
            GROUP BY group_code ORDER BY count(*) DESC, group_code LIMIT 3
        """, [lower, lower, upper, upper]).fetchall()
        groups = [row[0] for row in rows]
    series = ([{"id": "total", "label": "Total"}] if total else []) + [
        {"id": "group:" + code, "label": code} for code in groups
    ]
    queries, params = [], []
    if total:
        queries.append("SELECT 'total' AS series_id, bucket, value FROM filtered")
    if groups:
        queries.append(
            "SELECT 'group:' || group_code AS series_id, bucket, value FROM filtered "
            "WHERE group_code IN (SELECT * FROM unnest(?::VARCHAR[]))"
        )
        params.append(groups)
    if not queries:
        return series, "", []
    samples = f"""
        WITH selected AS ({" UNION ALL ".join(queries)}),
        samples AS (
            SELECT *, CASE WHEN (? IS NULL OR value >= ?) AND (? IS NULL OR value <= ?)
                            THEN value END AS selected_value FROM selected
        )
    """
    return series, samples, params + [lower, lower, upper, upper]


def add_summary(con, series, samples, params, aggregate, dates):
    summaries = {}
    rows = con.execute(samples + """
        SELECT series_id, count(selected_value), count(*) - count(value),
            count(value) - count(selected_value), avg(selected_value),
            stddev_samp(selected_value), min(selected_value), max(selected_value),
            quantile_cont(selected_value, [0.01, 0.5, 0.95, 0.99])
        FROM samples GROUP BY series_id
    """, params).fetchall()
    for key, n, missing, outside, mean, stddev, minimum, maximum, quantiles in rows:
        p01, median, p95, p99 = quantiles or (None, None, None, None)
        summaries[key] = {
            "n": n, "missing": missing, "outside": outside, "mean": mean,
            "stddev": stddev, "min": minimum, "max": maximum,
            "p01": p01, "median": median, "p95": p95, "p99": p99,
        }
    points = defaultdict(dict)
    for key, bucket, value, count in con.execute(samples + f"""
        SELECT series_id, bucket, {AGGREGATES[aggregate]}, count(selected_value)
        FROM samples GROUP BY series_id, bucket
    """, params).fetchall():
        points[key][bucket.isoformat()] = (value, count)
    empty = dict.fromkeys(SUMMARY_KEYS) | {"n": 0, "missing": 0, "outside": 0}
    for item in series:
        key = item["id"]
        item["summary"] = summaries.get(key, empty)
        aligned = [points[key].get(day, (None, 0)) for day in dates]
        item["time"] = [point[0] for point in aligned]
        item["timeCounts"] = [point[1] for point in aligned]


def bin_edges(left, right, bins):
    if left == right:
        return [left - 0.5, right + 0.5]
    width = (right - left) / bins
    return [left + width * index for index in range(bins)] + [right]


def add_distributions(con, series, samples, params, bins):
    nonempty = [s["summary"] for s in series if s["summary"]["n"]]
    if not nonempty:
        for item in series:
            item.update(histogram=[], centralHistogram=[], ecdfX=[], ecdfY=[])
        return [], []
    edges = bin_edges(min(s["min"] for s in nonempty), max(s["max"] for s in nonempty), bins)
    central_edges = bin_edges(min(s["p01"] for s in nonempty), max(s["p99"] for s in nonempty), bins)
    left, right = edges[0], edges[-1]
    frequencies = defaultdict(list)
    for key, value, count in con.execute(samples + """
        SELECT series_id, selected_value, count(*)
        FROM samples WHERE selected_value IS NOT NULL
        GROUP BY series_id, selected_value ORDER BY series_id, selected_value
    """, params).fetchall():
        frequencies[key].append((value, count))
    for item in series:
        counts = [0] * (len(edges) - 1)
        central_counts = [0] * (len(central_edges) - 1)
        x, y = [], []
        cumulative = 0
        total = item["summary"]["n"]
        for value, count in frequencies[item["id"]]:
            index = min(len(counts) - 1, bisect_right(edges, value) - 1)
            counts[index] += count
            if central_edges[0] <= value <= central_edges[-1]:
                index = min(len(central_counts) - 1, bisect_right(central_edges, value) - 1)
                central_counts[index] += count
            if not x:
                x.append(left)
                y.append(0.0)
            cumulative += count
            x.append(value)
            y.append(cumulative / total)
        if x and x[-1] < right:
            x.append(right)
            y.append(1.0)
        item.update(histogram=counts, centralHistogram=central_counts, ecdfX=x, ecdfY=y)
    return edges, central_edges


def evaluate(request, partitions):
    start, end, files, gaps = selection(request, partitions)
    signal = request.get("signal")
    grain = request.get("grain", "day")
    aggregate = request.get("aggregate", "mean")
    group = request.get("group", "")
    outcome = request.get("outcome", "all")
    choices = ((signal, SIGNALS), (grain, GRAINS), (aggregate, AGGREGATES),
               (group, ("", *GROUPS)), (outcome, OUTCOMES))
    if any(not isinstance(value, str) or value not in allowed for value, allowed in choices):
        raise ValueError("Choose valid signal, time interval, statistic and grouping options.")
    total = request.get("total", True)
    groups = request.get("groups", [])
    if not isinstance(total, bool) or (not total and not group):
        raise ValueError("Show Total or choose a comparison group.")
    if (not isinstance(groups, list) or len(groups) > MAX_GROUPS
            or any(not isinstance(code, str) or not 1 <= len(code) <= 12 for code in groups)
            or (groups and not group)):
        raise ValueError(f"Select up to {MAX_GROUPS} comparison groups.")
    groups = list(dict.fromkeys(groups))
    lower = optional_number(request.get("min"))
    upper = optional_number(request.get("max"))
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("The maximum signal value must be at least the minimum.")
    bins = request.get("bins", 30)
    if type(bins) is not int or not 5 <= bins <= 100:
        raise ValueError("Choose 5 to 100 histogram bins.")
    dates = calendar(start, end, grain)
    label, unit = SIGNALS[signal]
    with TemporaryDirectory(prefix="bts-signal-") as scratch:
        with duckdb.connect(config={"memory_limit": "1GB", "threads": "2",
                                    "temp_directory": scratch}) as con:
            load_filtered(con, request, files, start, end, signal, group, grain, outcome)
            series, samples, params = select_series(con, group, groups, total, (lower, upper))
            edges, central_edges = [], []
            if series:
                add_summary(con, series, samples, params, aggregate, dates)
                edges, central_edges = add_distributions(con, series, samples, params, bins)
    return {
        "label": label, "unit": unit, "signal": signal, "grain": grain,
        "aggregate": aggregate, "dates": dates, "series": series,
        "edges": edges, "centralEdges": central_edges,
        "missingMonths": gaps,
    }
