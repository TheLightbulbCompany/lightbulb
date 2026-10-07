import Link from "next/link";

import { listRecentTakedowns } from "@/app/admin/_actions/marketplace";
import { truncate } from "@/lib/utils";

export const metadata = { title: "Takedowns · Admin" };
// Always re-fetch on navigation so the audit-log view reflects takedowns
// granted from the listing detail page.
export const dynamic = "force-dynamic";

interface TakedownRow {
  listing_id: string;
  takedown_id: string;
  reason: string;
  filed_by_name?: string;
  filed_by_email?: string;
  basis_md?: string;
  decision?: string;
  decided_by?: string;
  decided_at?: string;
  affected_purchases?: number;
}

const BASIS_TRUNCATE = 240;

/**
 * Recent takedowns (audit-log view) — Server Component.
 *
 * Under the Isol8-internal scope there is no public takedown filing form,
 * so admins initiate takedowns directly from the listing detail page (see
 * `TakedownButton`). This page is the historical record: granted takedowns
 * ordered by `decided_at` desc, with no Grant button (already granted).
 *
 * Read-only — call `listRecentTakedowns()` (returns the `ActionResult`
 * envelope) and render. Errors render inline.
 */
export default async function RecentTakedownsPage() {
  const result = await listRecentTakedowns();
  if (!result.ok) {
    return (
      <p className="text-sm text-err" role="alert">
        Error loading takedowns: {result.error ?? `http_${result.status}`}
      </p>
    );
  }

  const items =
    (result.data as { items?: TakedownRow[] } | undefined)?.items ?? [];

  return (
    <div className="space-y-3">
      <h1 className="text-2xl font-semibold text-foreground">Takedowns</h1>
      <p className="text-sm text-muted-foreground">
        Recent takedowns. Granted by admins on the listing detail page.
      </p>
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">No takedowns recorded yet.</p>
      ) : (
        <ul className="space-y-3">
          {items.map((td) => (
            <li
              key={td.takedown_id}
              className="rounded-lg border border-border bg-card p-5"
            >
              <div className="mb-2 flex flex-wrap items-baseline justify-between gap-3">
                <p className="font-semibold text-foreground">
                  listing:{" "}
                  <Link
                    href={`/admin/marketplace/listings/${encodeURIComponent(td.listing_id)}`}
                    className="font-mono text-info hover:underline"
                  >
                    {td.listing_id}
                  </Link>
                </p>
                <span className="text-xs text-muted-foreground font-mono">
                  {td.decided_at ?? "—"}
                </span>
              </div>
              <dl className="grid grid-cols-2 gap-2 text-xs text-muted-foreground mb-3 sm:grid-cols-4">
                <div>
                  <dt className="text-muted-foreground">Reason</dt>
                  <dd className="text-foreground">{td.reason}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Decided by</dt>
                  <dd className="text-foreground font-mono break-all">
                    {td.decided_by ?? "—"}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Affected purchases</dt>
                  <dd className="text-foreground">
                    {td.affected_purchases ?? 0}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Status</dt>
                  <dd className="text-foreground">{td.decision ?? "—"}</dd>
                </div>
              </dl>
              {td.basis_md && (
                <p className="whitespace-pre-line rounded-sm bg-background p-3 text-sm text-foreground">
                  {truncate(td.basis_md ?? "", BASIS_TRUNCATE)}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
