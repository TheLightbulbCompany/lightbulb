/**
 * Parse an `isol8://agent/<agentId>` deep link (optionally with a query string).
 * Returns null for any other scheme/host (e.g. the oauth-native-callback link).
 */
export function parseAgentDeepLink(url: string): { agentId: string } | null {
  const match = /^isol8:\/\/agent\/([^/?#]+)/.exec(url);
  if (!match) return null;
  const agentId = decodeURIComponent(match[1]);
  return agentId ? { agentId } : null;
}
