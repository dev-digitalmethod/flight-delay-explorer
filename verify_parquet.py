"""Reproduce the October 2025 DOT Table 5 benchmark from Parquet."""

import argparse
import csv
from pathlib import Path

import duckdb

from analyze import BENCHMARK


def reconcile(parquet, report):
    with duckdb.connect() as con:
        national_count, outside_month = con.execute("""
            SELECT count(*),
                count(*) FILTER (WHERE FlightDate IS NULL OR
                    FlightDate NOT BETWEEN DATE '2025-10-01' AND DATE '2025-10-31')
            FROM read_parquet(?)
        """, [str(parquet)]).fetchone()
        if outside_month:
            raise ValueError("The benchmark requires the October 2025 partition.")
        results = con.execute("""
            WITH flights AS (
                SELECT Origin, Dest, Cancelled, Diverted, ArrDelay, DepDelay
                FROM read_parquet(?)
            )
            SELECT Dest AS airport, 'arrival' AS direction, count(*) AS reported,
                count(*) FILTER (WHERE NOT Cancelled AND NOT Diverted AND ArrDelay < 15) AS on_time
            FROM flights WHERE Dest IN ('RDU', 'CLT') GROUP BY Dest
            UNION ALL
            SELECT Origin AS airport, 'departure' AS direction, count(*) AS reported,
                count(*) FILTER (WHERE NOT Cancelled AND DepDelay < 15) AS on_time
            FROM flights WHERE Origin IN ('RDU', 'CLT') GROUP BY Origin
            ORDER BY airport, direction
        """, [str(parquet)]).fetchall()

    found = {(a, d): (n, round(100 * on_time / n, 1)) for a, d, n, on_time in results}
    matches = 0
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(("airport", "direction", "computed_count", "dot_count",
                         "computed_on_time_pct", "dot_on_time_pct", "match"))
        for (airport, direction), (dot_count, dot_pct) in sorted(BENCHMARK.items()):
            count, rate = found.get((airport, direction), (None, None))
            matches += (count == dot_count) + (rate == dot_pct)
            writer.writerow((airport, direction, count, dot_count, rate, dot_pct,
                             (count, rate) == (dot_count, dot_pct)))
    if matches != 8:
        raise ValueError(f"DOT Table 5 benchmark: {matches}/8 match. See {report}.")
    return f"October 2025 DOT check: 8/8 counts and rates match ({national_count:,} national rows)"


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("parquet", type=Path)
    cli.add_argument("--report", type=Path, default=Path("dot_table5_parquet.csv"))
    inputs = cli.parse_args()
    print(reconcile(inputs.parquet, inputs.report))
