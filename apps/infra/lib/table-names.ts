/** DynamoDB table-name prefix for an environment. */
export const tablePrefix = (env: string) => `lightbulb-${env}-`;

/** Non-table AWS resource names carry the same brand prefix as tables, so a rename moves ONE literal. */
export const resourcePrefix = (env: string) => `lightbulb-${env}-`;

/** Log group paths are path-shaped, not prefix-shaped. */
export const logGroupPath = (env: string, suffix: string) =>
  `/lightbulb/${env}/${suffix}`;

/** A table's name under the current prefix — for env vars and by-name grants. */
export const tableName = (env: string, suffix: string) =>
  `${tablePrefix(env)}${suffix}`;

/**
 * Names of the Spark data store (SparkStoreStack). ServiceStack builds its grants
 * from these instead of importing the stack, so a failed spark-store deploy cannot
 * hold the backend's deploy back. Glue names use underscores: Firehose rejects hyphens.
 */
// Mirrored in apps/backend/scripts/spark_store_ddl.py (and the Athena workgroup names in the backend). The task_steps table's stream is named "...-spark-steps".
export const sparkStoreNames = (env: string) => {
  const p = resourcePrefix(env);
  return {
    bucket: `${p}spark-store`,
    database: `lightbulb_${env}_spark`,
    streams: ["turns", "steps", "events"].map((s) => `${p}spark-${s}`),
    workgroup: `${p}spark`,
    adhocWorkgroup: `${p}spark-adhoc`,
    jobsWorkgroup: `${p}spark-jobs`,
    firehoseRole: `${p}spark-firehose`,
    logGroup: `/aws/firehose/${p}spark`,
  };
};
