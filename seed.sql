-- Seed for the Docker demo. Runs once, when the postgres volume is first created.
--
-- The goal is a database that is deliberately unhealthy in a way db-checker can
-- see, so the first run shows real findings instead of an empty report.

-- 200,000 rows, no primary key, no index - exactly how the real production_data
-- table ended up: built by a bulk insert, so it got no key and therefore no index.
CREATE TABLE public.readings AS
SELECT
    g                                        AS id,
    'MACHINE-' || (g % 40)                   AS machine,
    md5(random()::text)                      AS payload,
    now() - (g || ' minutes')::interval      AS taken_at
FROM generate_series(1, 200000) AS g;

-- A second one, wide, so bytes-per-row is large enough for the export-size
-- warning to trigger.
CREATE TABLE public.wide_rows AS
SELECT
    g                                                          AS id,
    repeat(md5(random()::text), 40)                            AS blob_a,
    repeat(md5(random()::text), 40)                            AS blob_b
FROM generate_series(1, 120000) AS g;

-- ANALYZE matters here, and it is worth understanding why.
--
-- The unindexed-tables check filters on pg_class.reltuples, which is the
-- planner's ROW ESTIMATE, not a count. Right after a bulk insert it is still -1,
-- so a freshly loaded table is invisible to the check until statistics exist.
--
-- That is not a flaw in the check - it is the same reason the planner makes bad
-- choices on a never-analysed table, which is what the stale_statistics check
-- looks for. Analysing one table and not the other lets you see both behaviours.
ANALYZE public.readings;

-- wide_rows is deliberately left un-analysed here, but do not expect it to stay
-- that way: autovacuum notices a table with no statistics and analyses it on its
-- own, usually within a couple of minutes. Run the checks IMMEDIATELY after
-- `docker compose up` to catch it, then run them again five minutes later and it
-- will have gone.
--
-- That disappearance is the more useful lesson. It is why stale_statistics rarely
-- fires on a healthy server, and why it means something when it does: autovacuum
-- is either switched off for that table or cannot keep up.
