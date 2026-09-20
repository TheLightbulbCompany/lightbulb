/** DynamoDB table-name prefix for an environment. */
export const tablePrefix = (env: string) => `lightbulb-${env}-`;

/**
 * Non-table AWS resource names carry the same brand prefix as tables.
 *
 * Kept beside the table helpers on purpose: a rename has to move ONE literal,
 * not the 147 scattered across the stacks. Spelling a renamed resource's name
 * out by hand in a second stack is what orphaned the bedrock-gate's pointer at
 * the invocation-dedup table -- the meter renamed the table via a helper, the
 * gate still named the old one as a string, and nothing failed until a trial
 * settle hit a table that no longer existed.
 */
export const resourcePrefix = (env: string) => `lightbulb-${env}-`;

/**
 * Alert topic ARN. The topics are DEFINED in observability-stack but named by
 * literal ARN in four other stacks, which cannot import them (that would make
 * observability a dependency of the gate and the meter). Six call sites, so
 * the name lives here rather than being spelled out six times.
 */
export const alertTopicArn = (
  region: string,
  account: string,
  env: string,
  tier: "page" | "warn",
) => `arn:aws:sns:${region}:${account}:${resourcePrefix(env)}alerts-${tier}`;

/** Log group paths are path-shaped, not prefix-shaped. */
export const logGroupPath = (env: string, suffix: string) =>
  `/lightbulb/${env}/${suffix}`;

/**
 * ARNs for EVERY table under a prefix, as a single wildcard pair.
 *
 * This replaces ~18 `grantReadWriteData` calls, and the reason is structural
 * rather than stylistic. `grant*()` on another stack's table creates a
 * cross-stack Export, and CloudFormation REFUSES TO CHANGE AN EXPORTED VALUE
 * WHILE ANYTHING IMPORTS IT:
 *
 *     Cannot update export isol8-dev-database:ExportsOutputRefUsageCountersTable…
 *     as it is in use by isol8-dev-observability
 *
 * which is exactly how the first attempt at the table rename failed. The repo
 * has been here before -- see credential-export-decouple.test.ts and "the
 * 2026-05-31 incident" it names. Granting by NAME creates no export, so the
 * tables become renameable.
 *
 * Enumerating all 22 tables instead is not an option: the backend task role's
 * inline policy is already 8,913 of IAM's 10,240-byte limit, and 22 tables x 2
 * ARNs would overrun it by ~1.5KB. One wildcard pair costs 2 entries and frees
 * roughly 2.9KB.
 *
 * The grant is scoped to this environment's current Lightbulb table namespace,
 * not `table/*` or a legacy prefix.
 */
export const allTableArns = (
  region: string,
  account: string,
  env: string,
): string[] => [
  `arn:aws:dynamodb:${region}:${account}:table/${tablePrefix(env)}*`,
  `arn:aws:dynamodb:${region}:${account}:table/${tablePrefix(env)}*/index/*`,
];

/** A table's name under the current prefix — for env vars and by-name grants. */
export const tableName = (env: string, suffix: string) =>
  `${tablePrefix(env)}${suffix}`;
