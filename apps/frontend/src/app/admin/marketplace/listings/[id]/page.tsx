import Link from "next/link";

import { getListingPreview } from "@/app/admin/_actions/marketplace";

import { ModerationActions } from "../ModerationActions";
import { TakedownButton } from "../TakedownButton";
import { formatBytes } from "@/lib/utils";

export const metadata = { title: "Listing review · Admin" };
export const dynamic = "force-dynamic";

interface SafetyFlag {
  pattern: string;
  severity: "high" | "medium" | "low";
  file: string;
  line: number | null;
  snippet: string;
}

interface FileTreeEntry {
  path: string;
  size_bytes: number;
}

interface OpenclawSummary {
  tools_count: number;
  providers: string[];
  cron_count: number;
  channels_count: number;
  sub_agent_count: number;
  raw_config_size_bytes: number;
}

interface ListingPreview {
  listing_id: string;
  version: number;
  slug: string;
  name: string;
  seller_id: string;
  format: "skillmd" | "openclaw";
  status: string;
  price_cents: number;
  tags: string[];
  manifest: Record<string, unknown>;
  file_tree: FileTreeEntry[];
  skill_md_text: string | null;
  openclaw_summary: OpenclawSummary | null;
  safety_flags: SafetyFlag[];
  /** Backend sets this when the artifact exceeds the in-memory scan cap.
   * File tree is still populated (from tar headers, cheap), but safety
   * scan didn't run — moderator must reject or ask the seller to slim
   * the artifact. Approval is gated server-side too. */
  scan_incomplete?: boolean;
  scan_failure_reason?: string | null;
  tarball_bytes?: number;
  unpacked_bytes?: number;
}

function severityBadge(s: SafetyFlag["severity"]): string {
  if (s === "high") return "border-err text-err";
  if (s === "medium") return "border-warn text-warn";
  return "border-border text-muted-foreground";
}

function flagsToRejectionPrefill(flags: SafetyFlag[]): string {
  const high = flags.filter((f) => f.severity === "high");
  if (high.length === 0) return "";
  const lines = ["Auto-flagged by safety scan:"];
  for (const f of high.slice(0, 10)) {
    const loc = f.line ? `${f.file}:${f.line}` : f.file;
    lines.push(`- ${f.pattern} at ${loc}: ${f.snippet}`);
  }
  if (high.length > 10) {
    lines.push(`… and ${high.length - 10} more high-severity findings`);
  }
  return lines.join("\n");
}

/**
 * Listing detail review page (Server Component).
 *
 * Calls /admin/marketplace/listings/{id}/preview which streams the listing's
 * S3 tarball, extracts in-memory, runs marketplace_safety.scan, and returns
 * the structured preview. The page renders:
 *   - Safety flags banner (red/amber by severity)
 *   - Metadata card
 *   - File tree
 *   - SKILL.md content (skillmd) OR openclaw summary (openclaw)
 *   - <ModerationActions> with rejection notes prefilled from high-severity flags
 */
export default async function ListingDetail({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ version?: string }>;
}) {
  const { id } = await params;
  const { version: versionParam } = await searchParams;
  // Browse-view links include ?version=N to pin the exact row (the same
  // listing_id can have a published v1 + review v2). Review-queue links
  // omit it and let the backend pick "most actionable" — for those
  // we forward no value so the heuristic kicks in.
  const versionNum =
    versionParam !== undefined && /^\d+$/.test(versionParam)
      ? Number(versionParam)
      : undefined;
  const result = await getListingPreview(
    id,
    versionNum !== undefined ? { version: versionNum } : {},
  );

  if (!result.ok) {
    return (
      <main className="space-y-4">
        <Link
          href="/admin/marketplace/listings"
          className="text-sm text-muted-foreground hover:text-foreground"
        >
          ← Back to review queue
        </Link>
        <p className="text-sm text-err" role="alert">
          Error loading preview: {result.error ?? `http_${result.status}`}
        </p>
      </main>
    );
  }

  const preview = result.data as ListingPreview;
  const high = preview.safety_flags.filter((f) => f.severity === "high");
  const medium = preview.safety_flags.filter((f) => f.severity === "medium");

  return (
    <main className="space-y-6 max-w-5xl">
      <div>
        <Link
          href="/admin/marketplace/listings"
          className="text-sm text-muted-foreground hover:text-foreground"
        >
          ← Back to review queue
        </Link>
      </div>

      {/* Scan-incomplete banner — shown when the artifact is too large to
          scan fully. Approve is disabled in ModerationActions; the file
          tree below shows what's bloating it so the moderator can decide
          whether to reject or ask the seller to slim the artifact. */}
      {preview.scan_incomplete && (
        <section
          className="rounded-lg border-l-2 border-warn bg-card p-4"
          role="alert"
          data-testid="scan-incomplete-banner"
        >
          <h2 className="text-lg font-semibold text-warn mb-1">
            Scan incomplete — approval disabled
          </h2>
          <p className="text-sm text-foreground">
            {preview.scan_failure_reason ?? "Artifact exceeds the in-memory scan cap."}
          </p>
          <p className="text-xs text-muted-foreground mt-2">
            Tarball: {formatBytes(preview.tarball_bytes ?? 0)} compressed
            {(preview.unpacked_bytes ?? 0) > 0 &&
              ` · ${formatBytes(preview.unpacked_bytes ?? 0)} unpacked (so far)`}
            . Reject + Takedown remain available.
          </p>
        </section>
      )}

      {/* Safety banner */}
      {high.length > 0 && (
        <section
          className="rounded-lg border-l-2 border-err bg-card p-4"
          role="alert"
        >
          <h2 className="text-lg font-semibold text-err mb-2">
            {high.length} high-severity safety flag{high.length === 1 ? "" : "s"}
          </h2>
          <ul className="space-y-1 text-sm text-foreground">
            {high.slice(0, 6).map((f, i) => (
              <li key={i} className="font-mono">
                <span className="font-bold">{f.pattern}</span> in {f.file}
                {f.line ? `:${f.line}` : ""} — {f.snippet}
              </li>
            ))}
            {high.length > 6 && (
              <li className="text-err">
                … and {high.length - 6} more (see file tree below)
              </li>
            )}
          </ul>
        </section>
      )}
      {high.length === 0 && medium.length > 0 && (
        <section className="rounded-lg border-l-2 border-warn bg-card p-4">
          <h2 className="text-sm font-semibold text-warn">
            {medium.length} medium-severity finding{medium.length === 1 ? "" : "s"}
          </h2>
          <p className="text-muted-foreground text-xs mt-1">
            Worth reviewing but not auto-blocking.
          </p>
        </section>
      )}

      {/* Metadata + Moderation actions */}
      <section className="rounded-lg border border-border bg-card p-5 flex items-start justify-between gap-6">
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-bold text-foreground">{preview.name}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            <code className="font-mono">{preview.slug}</code> · {preview.format} ·{" "}
            ${(preview.price_cents / 100).toFixed(2)} · status: {preview.status}
          </p>
          <p className="mt-2 text-xs text-muted-foreground">
            seller:{" "}
            <Link
              href={`/admin/users/${encodeURIComponent(preview.seller_id)}`}
              className="font-mono text-info hover:underline"
            >
              {preview.seller_id}
            </Link>
          </p>
          <div className="mt-3 flex flex-wrap gap-1">
            {preview.tags.map((t) => (
              <span
                key={t}
                className="text-xs px-2 py-0.5 rounded-sm bg-accent text-muted-foreground"
              >
                {t}
              </span>
            ))}
          </div>
        </div>
        {/* Approve / Reject only make sense while the listing is in the
            review queue. Backend gates both on status='review', so
            showing them on published / retired / taken_down / draft rows
            (the admin can land on this page from search or a direct URL)
            just produces confusing 409s. Hide instead. Takedown is its
            own section below and stays available for non-review states. */}
        {preview.status === "review" ? (
          <ModerationActions
            listingId={preview.listing_id}
            listingName={preview.name}
            slug={preview.slug}
            version={preview.version ?? 1}
            prefilledRejectionNotes={flagsToRejectionPrefill(preview.safety_flags)}
            approveDisabled={preview.scan_incomplete ?? false}
            approveDisabledReason={
              preview.scan_incomplete
                ? "Scan incomplete — approve blocked. Reject or ask the seller to slim the artifact."
                : undefined
            }
          />
        ) : (
          <div className="flex shrink-0 flex-col items-end gap-1 text-right">
            <span className="text-xs text-muted-foreground" data-testid="moderation-actions-hidden">
              No moderation actions —{" "}
              <code className="font-mono text-muted-foreground">{preview.status}</code>
            </span>
            <span className="text-caption text-muted-foreground max-w-[18rem]">
              {preview.status === "published"
                ? "Already live. Use Take down below to remove."
                : preview.status === "taken_down"
                  ? "Removed from the marketplace."
                  : preview.status === "retired"
                    ? "Superseded by a newer version."
                    : "Seller hasn't submitted for review yet."}
            </span>
          </div>
        )}
      </section>

      {/* Takedown — destructive action for the PUBLISHED version only. It is
          listing-scoped (takedown_service resolves the live version), so
          offering it on a retired/draft version page acted on a different
          version than the one shown, and a retired v1 of an already taken-down
          listing still offered it (double-fire). */}
      {preview.status === "published" && (
        <section className="rounded-lg border-l-2 border-err bg-card p-5 flex items-start justify-between gap-6">
          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-semibold text-err">
              Take down this listing
            </h2>
            <p className="mt-1 text-xs text-muted-foreground">
              Revokes every license for this listing, flips its status to
              {" "}<code className="font-mono">taken_down</code>, and is
              immediate. Use for policy violations, fraud, IP claims, or
              seller-initiated removal requests.
            </p>
          </div>
          <TakedownButton
            listingId={preview.listing_id}
            listingName={preview.name}
            slug={preview.slug}
          />
        </section>
      )}

      {/* File tree */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="text-sm font-semibold text-foreground mb-3">
          Files ({preview.file_tree.length})
        </h2>
        {preview.file_tree.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No artifact uploaded yet. The seller created the draft but hasn&apos;t
            attached files.
          </p>
        ) : (
          <ul className="space-y-1 text-sm font-mono text-muted-foreground">
            {preview.file_tree.map((f) => {
              const fileFlags = preview.safety_flags.filter((sf) => sf.file === f.path);
              return (
                <li key={f.path} className="flex items-center justify-between">
                  <span>{f.path}</span>
                  <span className="flex items-center gap-2">
                    {fileFlags.length > 0 && (
                      <span
                        className={`text-xs px-2 py-0.5 rounded-sm border ${severityBadge(
                          fileFlags[0].severity,
                        )}`}
                      >
                        {/* Rule 4: the severity is the badge's colour, so it
                            also has to be a word — never colour alone. */}
                        {fileFlags.length} {fileFlags[0].severity}
                      </span>
                    )}
                    <span className="text-muted-foreground">{formatBytes(f.size_bytes)}</span>
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {/* Content viewer */}
      {preview.format === "skillmd" && preview.skill_md_text != null && (
        <section className="rounded-lg border border-border bg-card p-5">
          <h2 className="text-sm font-semibold text-foreground mb-3">SKILL.md</h2>
          <pre className="text-xs text-foreground whitespace-pre-wrap font-mono bg-background p-3 rounded-sm overflow-x-auto">
            {preview.skill_md_text}
          </pre>
        </section>
      )}

      {preview.format === "openclaw" && preview.openclaw_summary && (
        <section className="rounded-lg border border-border bg-card p-5">
          <h2 className="text-sm font-semibold text-foreground mb-3">openclaw.json summary</h2>
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <dt className="text-muted-foreground">Tools</dt>
              <dd className="text-foreground">{preview.openclaw_summary.tools_count}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Providers</dt>
              <dd className="text-foreground font-mono">
                {preview.openclaw_summary.providers.join(", ") || "—"}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Cron jobs</dt>
              <dd className="text-foreground">{preview.openclaw_summary.cron_count}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Channels</dt>
              <dd className="text-foreground">{preview.openclaw_summary.channels_count}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Sub-agents</dt>
              <dd className="text-foreground">{preview.openclaw_summary.sub_agent_count}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Config size</dt>
              <dd className="text-foreground">
                {formatBytes(preview.openclaw_summary.raw_config_size_bytes)}
              </dd>
            </div>
          </dl>
        </section>
      )}

      {/* Manifest dump (always last, for completeness) */}
      <section className="rounded-lg border border-border bg-card p-5">
        <h2 className="text-sm font-semibold text-foreground mb-3">Manifest</h2>
        <pre className="text-xs text-muted-foreground whitespace-pre-wrap font-mono bg-background p-3 rounded-sm overflow-x-auto">
          {JSON.stringify(preview.manifest, null, 2)}
        </pre>
      </section>
    </main>
  );
}
