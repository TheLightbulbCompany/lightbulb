// Address-bar input → a navigable URL for the browser chrome's URL field.
// Ported verbatim from the OpenClaw fork's Control-UI helper
// (ui/src/components/browser/browser-url.ts) so the isol8 panel's URL bar
// coerces exactly like the container's own browser panel — bare hosts get
// https://, an explicit non-http(s) scheme is rejected, and a "host:port" is
// not mistaken for a scheme. Kept import-free + pure so it's unit-testable
// without mounting the panel (same discipline as browserViewUrl.ts).
export function normalizeBrowserUrlDraft(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed) {
    return null;
  }
  // A colon followed by digits is a port, not a scheme.
  const hasExplicitScheme = /^[a-z][a-z0-9+.-]*:(?![0-9])/i.test(trimmed);
  if (hasExplicitScheme && !/^https?:\/\//i.test(trimmed)) {
    return null;
  }
  const candidate = hasExplicitScheme ? trimmed : `https://${trimmed}`;
  try {
    const parsed = new URL(candidate);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}
