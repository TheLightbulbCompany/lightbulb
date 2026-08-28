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
export const legacyTablePrefix = (env: string) => `isol8-${env}-`;

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
