CREATE OR REPLACE VIEW {db}.events_v AS
SELECT * FROM (SELECT e.*, row_number() OVER (PARTITION BY row_id ORDER BY ts) AS rn FROM {db}.events e) WHERE rn = 1
