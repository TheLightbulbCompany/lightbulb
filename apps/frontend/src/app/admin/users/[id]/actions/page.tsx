import Link from "next/link";
import { auth } from "@clerk/nextjs/server";

import { getActions, getOverview } from "@/app/admin/_lib/api";
import { asAuditEntry, asRecord, asString } from "@/app/admin/_lib/narrow";
import { AuditRow } from "@/components/admin/AuditRow";
import { Card } from "@/components/admin/Card";
import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { WipeAccountDialog } from "@/components/admin/WipeAccountDialog";
import { WipeWorkspaceDialog } from "@/components/admin/WipeWorkspaceDialog";

export const metadata = { title: "Actions \u00b7 Admin" };

/** Mirrors billing/page.tsx's identity-email pick \u2014 kept local, same shape. */
function pickIdentityEmail(raw: unknown): string | null {
  const r = asRecord(raw);
  const list = r.email_addresses;
  if (!Array.isArray(list) || list.length === 0) return null;
  const first = asRecord(list[0]);
  return asString(first.email_address) ?? null;
}

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
        <h1 className="text-xl font-semibold text-foreground">Actions</h1>
        <ErrorBanner error="Missing Clerk session token." />
      </div>
    );
  }

  const [result, overview] = await Promise.all([
    getActions(token, { target_user_id: id, limit: 100, cursor }),
    getOverview(token, id),
  ]);

  const entries = (result.items ?? []).map(asAuditEntry);
  const email = overview ? pickIdentityEmail(overview.identity) : null;

  const nextCursorHref = result.cursor
    ? `/admin/users/${encodeURIComponent(id)}/actions?cursor=${encodeURIComponent(result.cursor)}`
    : null;

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-foreground">Admin actions</h1>

      <Card title="Danger zone">
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground">
            Permanently deletes every resource for this owner — runtime and
            S3 profiles, Paperclip, Bedrock credits, billing, and the Clerk
            user. Cannot be undone.
          </p>
          <WipeAccountDialog userId={id} email={email} />
        </div>
        {overview?.org ? (
          <div className="mt-4 space-y-2">
            <p className="text-xs text-muted-foreground">
              Wipes this member&apos;s whole workspace ({overview.org.name || overview.org.id}) and
              its members, layer by layer. Billing is kept unless explicitly included.
            </p>
            <WipeWorkspaceDialog orgId={overview.org.id} />
          </div>
        ) : null}
      </Card>

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
            className="rounded-md border border-border bg-card px-4 py-2 text-sm text-foreground hover:bg-accent"
          >
            Load more
          </Link>
        </div>
      ) : null}
    </div>
  );
}
