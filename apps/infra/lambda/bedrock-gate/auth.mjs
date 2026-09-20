// Bearer-token auth + AIP ownership. The container authenticates with a
// per-owner opaque token (AWS_BEARER_TOKEN_BEDROCK for OpenClaw,
// ANTHROPIC_AUTH_TOKEN for Claude Code). We store only its SHA-256 and look the
// owner up by that hash. SigV4 is deliberately NOT accepted — it is verifiable
// only by AWS (symmetric HMAC over the caller's secret), so an inbound SigV4
// header would be unverifiable decoration.
import { createHash } from "node:crypto";
import { readS, readBool, readMapS } from "./ddb.mjs";
import { unauthorized, denied, forbiddenAip } from "./errors.mjs";

export const hashToken = (token) => createHash("sha256").update(token, "utf8").digest("hex");

function bearerFrom(headers) {
  // Function URL lowercases header keys; be defensive anyway.
  const raw = headers?.authorization ?? headers?.Authorization ?? "";
  const m = /^Bearer\s+(.+)$/i.exec(raw.trim());
  return m ? m[1].trim() : null;
}

/**
 * Resolve the caller from the bearer token.
 * The token row (isol8-{env}-bedrock-gate-tokens) carries owner_id, the deny
 * flag (the kill switch), and aip_tiers { <aip-arn>: <tier> } written at mint —
 * so auth + AIP-ownership + tier + pricing all come from ONE GetItem.
 * @returns {{ownerId: string, aipTiers: Record<string,string>}}
 * @throws {GateError} 401 missing/unknown, 403 denied
 */
export async function authenticate(headers, ddb, env, { bedrock = true } = {}) {
  const token = bearerFrom(headers);
  if (!token) throw unauthorized();
  const res = await ddb.getItem({
    TableName: env.TOKENS_TABLE,
    ConsistentRead: true,
    Key: { token_hash: { S: hashToken(token) } },
  });
  const item = res?.Item;
  if (!item) throw unauthorized();
  if (readBool(item.denied) || (bedrock && readBool(item.bedrock_denied))) throw denied();
  const ownerId = readS(item.owner_id);
  if (!ownerId) throw unauthorized();
  return { ownerId, aipTiers: readMapS(item.aip_tiers) };
}

/**
 * The path modelId (URL-decoded AIP ARN) must belong to this owner; returns its
 * tier for pricing. Cross-tenant invocation is impossible — the caller can only
 * name an ARN present in its own token row's aip_tiers.
 * @throws {GateError} 403 if the model isn't the owner's
 */
export function tierForModel(modelId, aipTiers) {
  const tier = aipTiers?.[modelId];
  if (!tier) throw forbiddenAip();
  return tier;
}
