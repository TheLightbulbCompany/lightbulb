/**
 * Runtime narrowers for `unknown`-typed JSON payloads in the admin pages.
 *
 * The admin user-detail pages fetch DDB rows shaped as `unknown` (from the
 * backend admin-service composer), then narrow individual fields before
 * rendering. The three narrowers below were each defined inline in 3 pages
 * with identical bodies; consolidating prevents drift if the shape rules
 * ever need to tighten.
 */

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

export function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
