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
{props}
