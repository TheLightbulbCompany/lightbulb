/**
 * DynamoDB table-name prefixes, during the isol8 -> lightbulb rename.
 *
 * DynamoDB has no rename. Moving to a new prefix means CREATING new tables and
 * copying the items, so for a window BOTH prefixes exist and the fleet must be
 * able to reach either:
 *
 *   step A (this)  tableName -> NEW prefix, so CloudFormation creates and owns
 *                  the new tables. IAM widened to BOTH prefixes. Every READER
 *                  stays on the legacy prefix, so nothing changes behaviour.
 *   step B         copy the items into the new tables.
 *   step C         flip every reader ATOMICALLY -- the backend's
 *                  DYNAMODB_TABLE_PREFIX and the gate/meter Lambdas' own
 *                  *_TABLE env vars, which name tables independently. Flipping
 *                  the backend alone would leave bedrock-gate enforcing the
 *                  trial spend cap against a stale billing table.
 *   step D         delete the retained legacy tables.
 *
 * IAM has to span both prefixes for the whole window: the grants are written as
 * literal ARNs, so unlike a CDK `grantReadWriteData` they do NOT follow a
 * tableName change on their own.
 */
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
 * A table's ARNs spanning BOTH prefixes, for IAM statements that must survive
 * the migration window.
 *
 * The prefix is a WILDCARD rather than two enumerated ARNs, and that is a size
 * decision with a hard edge behind it. Listing both prefixes explicitly doubles
 * every entry, which took the backend task role's inline policy to 8,846 of
 * IAM's 10,240-byte limit -- 1.4KB of headroom, and already over it once
 * region/account render as tokens rather than literals. The next statement
 * anyone adds would fail the deploy with LimitExceeded.
 *
 * `table/*-{env}-{suffix}` keeps the entry count exactly as it was before the
 * migration. It is scoped to the exact table suffix AND env, in an account
 * where we are the only party creating tables, so the widening is bounded --
 * it does not admit a different table, only a different prefix on the same one.
 *
 * Narrow this back to `tablePrefix(env)` at step D, when the legacy tables go.
 */
export const bothTableArns = (
  region: string,
  account: string,
  env: string,
  suffix: string,
): string[] => [
  `arn:aws:dynamodb:${region}:${account}:table/*-${env}-${suffix}`,
  `arn:aws:dynamodb:${region}:${account}:table/*-${env}-${suffix}/index/*`,
];

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
 * The widening is small and bounded: the backend already held individual grants
 * on 22 of the 25 tables in this account, and nothing but us creates tables
 * here. It is scoped to the env-prefixed namespace, not `table/*`.
 */
export const allTableArns = (
  region: string,
  account: string,
  env: string,
): string[] => [
  `arn:aws:dynamodb:${region}:${account}:table/*-${env}-*`,
  `arn:aws:dynamodb:${region}:${account}:table/*-${env}-*/index/*`,
];

/** A table's name under the current prefix — for env vars and by-name grants. */
export const tableName = (env: string, suffix: string) =>
  `${tablePrefix(env)}${suffix}`;
