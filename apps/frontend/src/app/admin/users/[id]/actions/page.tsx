import Link from "next/link";
import { auth } from "@clerk/nextjs/server";

import { getActions } from "@/app/admin/_lib/api";
import { asAuditEntry } from "@/app/admin/_lib/narrow";
import { AuditRow } from "@/components/admin/AuditRow";
import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";

export const metadata = { title: "Actions \u00b7 Admin" };

interface ActionsPageProps {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

function parseCursor(input: string | string[] | undefined): string | undefined {
  const v = Array.isArray(input) ? input[0] : input;
  return v && v.length > 0 ? v : undefined;
}

export default async function ActionsPage({
  params,
  searchParams,
}: ActionsPageProps) {
  const { id } = await params;
  const sp = await searchParams;
  const cursor = parseCursor(sp.cursor);

  const { getToken } = await auth();
  const token = await getToken();
  if (!token) {
    return (
      <div className="space-y-6">
        <h1 className="text-xl font-semibold text-zinc-100">Actions</h1>
        <ErrorBanner error="Missing Clerk session token." />
      </div>
    );
  }

  const result = await getActions(token, {
    target_user_id: id,
    limit: 100,
    cursor,
  });

  const entries = (result.items ?? []).map(asAuditEntry);

  const nextCursorHref = result.cursor
    ? `/admin/users/${encodeURIComponent(id)}/actions?cursor=${encodeURIComponent(result.cursor)}`
    : null;

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-zinc-100">Admin actions</h1>

      {entries.length === 0 ? (
        <EmptyState
          title="No admin actions"
          body="No admin has taken any action against this user yet."
        />
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
