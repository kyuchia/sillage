-- Classify the bus "teleports" counted by qa/data_quality.py, for one day.
--
--   psql sillage < qa/teleport_kinds.sql        (~1 minute; read-only)
--
-- fetched_at is when the fetcher polled, not when the bus's GPS reported; the STM
-- feed's per-vehicle timestamp is not stored. So a fast pair of fixes is either a
-- real GPS error, or a position the feed held stale for a few polls and then caught
-- up. The two want different handling, so they are counted separately:
--
--   spike: jump out / jump back   out and straight back: a GPS error
--   catch-up after frozen fix     the two fixes before the jump are identical
--   one-way jump                  everything else
--
-- Verified against the planted ~39 km spike (bus B8) in tests/fixtures/seed.sql.
WITH p AS (
  SELECT vehicle_id AS uid, fetched_at, geom
  FROM vehicle_positions
  WHERE fetched_at >= '2026-08-20 00:00-04' AND fetched_at < '2026-08-21 00:00-04'
    AND longitude BETWEEN -75.0 AND -72.5 AND latitude BETWEEN 44.5 AND 46.5
), s AS (
  SELECT fetched_at, geom,
         LAG(geom)    OVER w AS prev_g,
         LAG(geom, 2) OVER w AS prev2_g,
         LEAD(geom)   OVER w AS next_g,
         EXTRACT(EPOCH FROM fetched_at - LAG(fetched_at) OVER w) AS dt
  FROM p WINDOW w AS (PARTITION BY uid ORDER BY fetched_at)
), j AS (
  SELECT *, ST_Distance(geom, prev_g) AS d FROM s WHERE dt > 0
)
SELECT CASE
         WHEN prev2_g IS NOT NULL AND ST_Distance(geom, prev2_g) < 0.2 * d THEN 'spike: jump back'
         WHEN next_g  IS NOT NULL AND ST_Distance(prev_g, next_g) < 0.2 * d THEN 'spike: jump out'
         WHEN prev2_g IS NOT NULL AND ST_Distance(prev2_g, prev_g) < 1     THEN 'catch-up after frozen fix'
         ELSE 'one-way jump'
       END AS kind,
       COUNT(*) AS n,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY d)::numeric)  AS median_metres,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY dt)::numeric) AS median_seconds
FROM j
WHERE d / dt * 3.6 > 150
GROUP BY 1 ORDER BY n DESC;
