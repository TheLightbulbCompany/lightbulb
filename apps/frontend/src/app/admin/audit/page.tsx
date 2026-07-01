import Link from "next/link";
import { auth } from "@clerk/nextjs/server";

import { getActions, type AdminAction } from "@/app/admin/_lib/api";
import { AuditRow, type AuditEntry } from "@/components/admin/AuditRow";
import { Card } from "@/components/admin/Card";

export const metadata = { title: "Platform audit · Admin" };

// ---------------------------------------------------------------------------
// AdminAction (intentionally permissive in `_lib/api.ts`) → AuditEntry (the
// strict shape `<AuditRow>` expects). Mirrors `users/[id]/actions/page.tsx`;
// defaults to safe values when the backend hasn't filled a field.
// ---------------------------------------------------------------------------

function asAuditEntry(raw: AdminAction): AuditEntry {
  const compositeKey =
    typeof (raw as Record<string, unknown>).timestamp_action_id === "string"
      ? ((raw as Record<string, unknown>).timestamp_action_id as string)
      : raw.timestamp && raw.action_id
        ? `${raw.timestamp}#${raw.action_id}`
        : (raw.timestamp ?? "");

  const result =
    raw.status === "error" || raw.status === "success"
      ? raw.status
      : (raw as Record<string, unknown>).result === "error" ||
          (raw as Record<string, unknown>).result === "success"
        ? ((raw as Record<string, unknown>).result as "success" | "error")
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

interface AuditPageProps {
  searchParams: Promise<{ admin_user_id?: string; cursor?: string }>;
}

export default async function PlatformAuditPage({ searchParams }: AuditPageProps) {
  const params = await searchParams;
  const { getToken } = await auth();
  const token = await getToken();
  const result = token
    ? await getActions(token, {
        admin_user_id: params.admin_user_id,
        limit: 100,
        cursor: params.cursor,
      })
    : { items: [], cursor: null };

  const entries = (result.items ?? []).map(asAuditEntry);

  const nextQuery = new URLSearchParams();
  if (params.admin_user_id) nextQuery.set("admin_user_id", params.admin_user_id);
  if (result.cursor) nextQuery.set("cursor", result.cursor);
  const nextCursorHref = result.cursor ? `/admin/audit?${nextQuery.toString()}` : null;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold text-zinc-100">Platform audit</h1>
      <Card>
        {entries.length === 0 ? (
          <p className="text-sm text-zinc-500">No admin actions recorded.</p>
        ) : (
          <div className="space-y-1.5">
            {entries.map((entry) => (
              <AuditRow
                key={`${entry.timestamp_action_id}-${entry.action}`}
                entry={entry}
              />
            ))}
          </div>
        )}
      </Card>
      {nextCursorHref ? (
        <div className="flex justify-center">
          <Link
            href={nextCursorHref}
            className="rounded-md border border-white/10 bg-white/[0.04] px-4 py-2 text-sm text-zinc-100 hover:bg-white/[0.08]"
          >
            Load more
          </Link>
        </div>
      ) : null}
    </div>
  );
}
