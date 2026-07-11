/**
 * Parse an `isol8://agent/<agentId>` deep link (optionally with a query string).
 * Returns null for any other scheme/host (e.g. the oauth-native-callback link).
 */
export function parseAgentDeepLink(url: string): { agentId: string } | null {
  const match = /^isol8:\/\/agent\/([^/?#]+)/.exec(url);
  if (!match) return null;
  // Deep links are externally triggerable, so a malformed percent-encoding
  // (e.g. `isol8://agent/%`) must return null rather than throw a URIError.
  let agentId: string;
  try {
    agentId = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  return agentId ? { agentId } : null;
}
