import type { AuditEntry } from "@/components/admin/AuditRow";
import type { LogEntry } from "@/components/admin/LogRow";

import type { AdminAction } from "./api";

/**
 * Runtime narrowers for `unknown`-typed JSON payloads in the admin pages.
 *
 * The admin user-detail pages fetch DDB rows shaped as `unknown` (from the
 * backend admin-service composer), then narrow individual fields before
 * rendering. The narrowers below were each defined inline in multiple pages
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

/**
 * AdminAction (intentionally permissive in `api.ts`) → AuditEntry (the strict
 * shape `<AuditRow>` expects). Defaults to safe values when the backend
 * hasn't filled a field. Shared by the platform audit page and the per-user
 * actions tab.
 */
export function asAuditEntry(raw: AdminAction): AuditEntry {
  // Backend uses `timestamp_action_id` as the composite sort key, but the
  // permissive type uses both `timestamp` and `action_id`. Compose the
  // composite if it isn't already present.
  const compositeKey =
    typeof (raw as Record<string, unknown>).timestamp_action_id === "string"
      ? ((raw as Record<string, unknown>).timestamp_action_id as string)
      : raw.timestamp && raw.action_id
        ? `${raw.timestamp}#${raw.action_id}`
        : (raw.timestamp ?? "");

  const RESULTS: readonly AuditEntry["result"][] = ["success", "error", "partial_failure"];
  const rawResult = (raw as Record<string, unknown>).result;
  const result =
    raw.status === "error" || raw.status === "success"
      ? raw.status
      : RESULTS.includes(rawResult as AuditEntry["result"])
        ? (rawResult as AuditEntry["result"])
        : "success";

  const auditStatus =
    (raw as Record<string, unknown>).audit_status === "panic" ||
    (raw as Record<string, unknown>).audit_status === "written"
      ? ((raw as Record<string, unknown>).audit_status as "written" | "panic")
      : "written";

  const httpStatus =
    typeof (raw as Record<string, unknown>).http_status === "number"
      ? ((raw as Record<string, unknown>).http_status as number)
      : 0;

  const elapsedMs =
    typeof (raw as Record<string, unknown>).elapsed_ms === "number"
      ? ((raw as Record<string, unknown>).elapsed_ms as number)
      : 0;

  const errorMessage =
    typeof (raw as Record<string, unknown>).error_message === "string"
      ? ((raw as Record<string, unknown>).error_message as string)
      : undefined;

  return {
    admin_user_id: raw.admin_user_id ?? "",
    timestamp_action_id: compositeKey,
    target_user_id: raw.target_user_id ?? "",
    action: raw.action ?? "",
    result,
    audit_status: auditStatus,
    http_status: httpStatus,
    elapsed_ms: elapsedMs,
    error_message: errorMessage,
  };
}

/**
 * Unknown CloudWatch event payload → LogEntry (the strict shape `<LogRow>`
 * expects). Shared by the per-user logs and activity tabs.
 */
export function asLogEntry(raw: unknown): LogEntry {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    timestamp: typeof r.timestamp === "string" ? r.timestamp : "",
    level:
      typeof r.level === "string" || r.level === null
        ? (r.level as LogEntry["level"])
        : null,
    message: typeof r.message === "string" ? r.message : "",
    correlation_id:
      typeof r.correlation_id === "string" ? r.correlation_id : null,
    raw_json:
      r.raw_json && typeof r.raw_json === "object"
        ? (r.raw_json as Record<string, unknown>)
        : null,
  };
}
