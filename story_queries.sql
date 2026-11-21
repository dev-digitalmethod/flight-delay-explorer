-- Run from the repository root against the original monthly Parquet partitions.
CREATE OR REPLACE TEMP VIEW story_flights AS
SELECT FlightDate, Origin, Reporting_Airline, DepDelay, Cancelled
FROM read_parquet('data/bts/flights/flight_year=*/flight_month=*/data.parquet',
                  hive_partitioning = false);

-- Compare RDU's month-by-month experience with the national calendar-month average.
SELECT month(FlightDate) AS flight_month,
       round(avg(DepDelay), 2) AS national_2022_to_2025_minutes,
       round(avg(DepDelay) FILTER (WHERE Origin = 'RDU' AND year(FlightDate) = 2024), 2)
         AS rdu_2024_minutes
FROM story_flights
WHERE year(FlightDate) BETWEEN 2022 AND 2025
GROUP BY 1 ORDER BY 1;

-- Compare the Windows outage dates with adjacent days and separate Delta (DL).
SELECT FlightDate, coalesce(Reporting_Airline, 'All carriers') AS carrier,
       count(*) AS reported_flights,
       count(*) FILTER (WHERE Cancelled) AS cancelled_flights,
       round(100.0 * cancelled_flights / reported_flights, 2) AS cancelled_pct,
       count(DepDelay) AS flights_with_delay_value,
       round(avg(DepDelay), 2) AS mean_delay_minutes
FROM story_flights
WHERE FlightDate BETWEEN DATE '2024-07-17' AND DATE '2024-07-25'
GROUP BY GROUPING SETS ((FlightDate), (FlightDate, Reporting_Airline))
HAVING grouping(Reporting_Airline) = 1 OR Reporting_Airline = 'DL'
ORDER BY 1, 2;
