CREATE TABLE IF NOT EXISTS {db}.task_steps (
  row_id string,
  ts timestamp,
  dt date,
  user_bucket int,
  user_id string,
  task_id string,
  session_id string,
  source string,
  automation_id string,
  step_no int,
  kind string,
  tool string,
  app string,
  input string,
  result string,
  ok boolean,
  ms int,
  status string,
  text_kept boolean,
  platform string,
  app_version string
)
PARTITIONED BY (dt, user_bucket)
LOCATION 's3://{bucket}/warehouse/task_steps/'
TBLPROPERTIES (
  'table_type'='ICEBERG',
  'format'='PARQUET',
  'write_compression'='ZSTD',
  'optimize_rewrite_delete_file_threshold'='1',
  'vacuum_max_snapshot_age_seconds'='86400',
  'vacuum_max_metadata_files_to_keep'='20'
)
