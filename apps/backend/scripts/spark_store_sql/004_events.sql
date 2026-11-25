CREATE TABLE IF NOT EXISTS {db}.events (
  row_id string,
  ts timestamp,
  dt date,
  user_bucket int,
  distinct_id string,
  user_id string,
  install_id string,
  event string,
  properties string,
  platform string,
  app_version string,
  environment string
)
PARTITIONED BY (dt, user_bucket)
LOCATION 's3://{bucket}/warehouse/events/'
TBLPROPERTIES (
  'table_type'='ICEBERG',
  'format'='PARQUET',
  'write_compression'='ZSTD',
  'optimize_rewrite_delete_file_threshold'='1',
  'vacuum_max_snapshot_age_seconds'='86400',
  'vacuum_max_metadata_files_to_keep'='20'
)
