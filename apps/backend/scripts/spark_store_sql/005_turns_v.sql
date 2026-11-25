CREATE OR REPLACE VIEW {db}.turns_v AS
SELECT * FROM (SELECT t.*, row_number() OVER (PARTITION BY row_id ORDER BY ts) AS rn FROM {db}.turns t) WHERE rn = 1
