CREATE OR REPLACE VIEW {db}.task_steps_v AS
SELECT * FROM (SELECT s.*, row_number() OVER (PARTITION BY row_id ORDER BY text_kept DESC, ts) AS rn FROM {db}.task_steps s) WHERE rn = 1
