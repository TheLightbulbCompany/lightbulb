import Link from "next/link";
import { auth } from "@clerk/nextjs/server";

import { getActions } from "@/app/admin/_lib/api";
import { asAuditEntry } from "@/app/admin/_lib/narrow";
import { AuditRow } from "@/components/admin/AuditRow";
import { Card } from "@/components/admin/Card";

export const metadata = { title: "Platform audit · Admin" };

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
      <h1 className="text-2xl font-semibold text-foreground">Platform audit</h1>
      <Card>
        {entries.length === 0 ? (
          <p className="text-sm text-muted-foreground">No admin actions recorded.</p>
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
            className="rounded-md border border-border bg-card px-4 py-2 text-sm text-foreground hover:bg-accent"
          >
            Load more
          </Link>
        </div>
      ) : null}
    </div>
  );
}
