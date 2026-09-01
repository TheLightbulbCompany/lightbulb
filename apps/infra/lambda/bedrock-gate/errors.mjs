// Gateway errors, shaped as Bedrock runtime errors so OpenClaw's provider and
// Claude Code surface them the way they'd surface a real Bedrock error. The
// container never learns it's talking to a proxy.

export class GateError extends Error {
  /**
   * @param {number} status  HTTP status to return to the container.
   * @param {string} bedrockType  the `__type` a real Bedrock error would carry.
   * @param {string} message  human-readable; agents/logs read this.
   * @param {string} [code]  isol8 gate code for the x-isol8-gate-code header + metrics.
   */
  constructor(status, bedrockType, message, code) {
    super(message);
    this.name = "GateError";
    this.status = status;
    this.bedrockType = bedrockType;
    this.code = code || null;
  }
  body() {
    return JSON.stringify({ __type: this.bedrockType, message: this.message });
  }
  headers() {
    const h = { "content-type": "application/json" };
    if (this.code) h["x-isol8-gate-code"] = this.code;
    return h;
  }
}

// Auth / authorization
export const unauthorized = () =>
  new GateError(401, "UnrecognizedClientException", "Lightbulb: missing or invalid gateway token", "auth_failed");
export const denied = () =>
  new GateError(403, "AccessDeniedException", "Lightbulb: access disabled for this account", "denied");
export const forbiddenAip = () =>
  new GateError(403, "AccessDeniedException", "Lightbulb: model not authorized for this account", "aip_forbidden");
export const forbiddenOp = () =>
  new GateError(403, "AccessDeniedException", "Lightbulb: operation not permitted", "op_forbidden");

// Budget
export const trialCap = () =>
  new GateError(429, "ThrottlingException", "Lightbulb: daily free limit reached — resets at midnight UTC", "trial_daily_cap");
export const trialExpired = () =>
  new GateError(429, "ThrottlingException", "Lightbulb: free trial ended — upgrade to keep going", "trial_expired");
export const creditExhausted = () =>
  new GateError(429, "ThrottlingException", "Lightbulb: credit balance exhausted", "credit_exhausted");
// DISTINCT from creditExhausted on purpose. "Drained their balance" is normal
// business; "has no ledger at all" is a provisioning failure on our side. They
// emit different metrics (`gate.credit_exhausted` vs
// `gate.ledger_unprovisioned`), so a broken provisioning deploy shows up as
// its own signal instead of hiding inside a counter that rises whenever
// customers legitimately run out of credits.
export const ledgerUnprovisioned = () =>
  new GateError(
    429,
    "ThrottlingException",
    "Lightbulb: billing is not set up for this account — contact support@thelightbulb.company",
    "ledger_unprovisioned",
  );

// Routing / internal
export const notFound = () =>
  new GateError(404, "ResourceNotFoundException", "Lightbulb: unknown route", "not_found");
export const internal = () =>
  new GateError(502, "InternalServerException", "Lightbulb: gate error", "gate_error");
