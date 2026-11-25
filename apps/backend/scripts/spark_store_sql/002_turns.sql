CREATE TABLE IF NOT EXISTS {db}.turns (
  row_id string,
  ts timestamp,
  dt date,
  user_bucket int,
  user_id string,
  session_id string,
  task_id string,
  role string,
  source string,
  text string,
  platform string,
  app_version string
)
PARTITIONED BY (dt, user_bucket)
LOCATION 's3://{bucket}/warehouse/turns/'
TBLPROPERTIES (
  'table_type'='ICEBERG',
  'format'='PARQUET',
  'write_compression'='ZSTD',
  'optimize_rewrite_delete_file_threshold'='1',
  'vacuum_max_snapshot_age_seconds'='86400',
  'vacuum_max_metadata_files_to_keep'='20'
)
