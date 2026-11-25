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
{props}
