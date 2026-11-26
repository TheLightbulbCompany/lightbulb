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

/** Name of the Spark data store bucket (SparkStoreStack). ServiceStack builds its grants from this instead of
 * importing the stack, so a failed spark-store deploy cannot hold the backend's deploy back. */
export const sparkStoreNames = (env: string) => ({ bucket: `${resourcePrefix(env)}spark-store` });
