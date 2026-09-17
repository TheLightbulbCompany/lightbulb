"use client";

// In-app skill-registry search + one-click install over `skills.search` /
// `skills.install`. WHICH registry depends on the runtime: OpenClaw answers from
// ClawHub, Hermes from its own skills hub (`hermes skills search`) — a different
// registry with a different row shape, so the `hub` prop picks both the wire
// shape and the copy. Was `ClawHubSearch`; the name was only true on ECS.

import { useEffect, useRef, useState } from "react";
import { Loader2, Download, Search, AlertTriangle, ExternalLink, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { capture } from "@/lib/analytics";

export type SkillHub = "clawhub" | "hermes";

// Mirrors OpenClaw ClawHubSkillSearchResult (src/infra/clawhub.ts).
interface ClawHubResult {
  slug: string;
  displayName: string;
  summary?: string;
  version?: string;
}

// Hermes's hub row (bridge `skills.search` → tools.skills_hub_search.unified_search).
interface HermesHubResult {
  name: string;
  description?: string;
  identifier: string;
  source?: string;
  trust?: string;
}

// One row shape for the list whichever registry produced it. `id` is both the
// React key and what `skills.install` is handed back.
interface HubRow {
  id: string;
  title: string;
  summary?: string;
  badge?: string;
}

export function toHubRows(hub: SkillHub, results: unknown[]): HubRow[] {
  if (hub === "hermes") {
    return (results as HermesHubResult[])
      .filter((r) => r?.identifier)
      .map((r) => ({
        id: r.identifier,
        title: r.name || r.identifier,
        summary: r.description,
        badge: r.trust || r.source,
      }));
  }
  return (results as ClawHubResult[])
    .filter((r) => r?.slug)
    .map((r) => ({
      id: r.slug,
      title: r.displayName || r.slug,
      summary: r.summary,
      badge: r.version ? `v${r.version}` : undefined,
    }));
}

const HUB_LABEL: Record<SkillHub, string> = {
  clawhub: "ClawHub",
  hermes: "Hermes skills hub",
};

type CallRpc = <T = unknown>(
  method: string,
  params?: Record<string, unknown>,
  timeoutMs?: number,
) => Promise<T>;

// The gateway rejects a risky/moderated install with a trust error the operator
// must explicitly acknowledge. We can't see a structured code through the RPC
// error, so match the surfaced message and offer an explicit "install anyway".
const TRUST_ERROR = /risk|trust|moderat|scan|blocked|acknowledge|verdict/i;

// A ClawHub install fetches + scans + unpacks the skill, which can outlast the
// gateway's 30s default RPC timeout; without a longer client timeout a still-
// running install surfaces as a false failure.
const INSTALL_TIMEOUT_MS = 120_000;

export function SkillHubSearch({
  query,
  callRpc,
  onInstalled,
  hub = "clawhub",
}: {
  query: string;
  callRpc: CallRpc;
  onInstalled: () => void;
  hub?: SkillHub;
}) {
  const [results, setResults] = useState<HubRow[] | null>(null);
  const [searchedFor, setSearchedFor] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [installing, setInstalling] = useState<string | null>(null);
  const [installed, setInstalled] = useState<Set<string>>(new Set());
  const [riskySlug, setRiskySlug] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ slug: string; text: string; kind: "ok" | "err" } | null>(null);

  const trimmed = query.trim();

  // Every search (and every query change) bumps this. A search response only
  // applies if its id is still current, so an in-flight request that resolves
  // after the query changed can't repopulate stale rows.
  const requestIdRef = useRef(0);

  // Results belong to the query they were fetched for. When the Skill Store
  // filter changes, invalidate any in-flight search and drop the previous
  // results (and their transient install state) so stale rows can't be shown —
  // or installed — under a prompt they no longer match.
  useEffect(() => {
    requestIdRef.current += 1;
    setResults(null);
    setSearchedFor("");
    setError(null);
    setRiskySlug(null);
    setMsg(null);
    setLoading(false);
  }, [query]);

  const runSearch = async () => {
    if (!trimmed) return;
    const reqId = (requestIdRef.current += 1);
    setLoading(true);
    setError(null);
    setResults(null);
    setRiskySlug(null);
    setMsg(null);
    try {
      const res = await callRpc<{ results?: unknown[] }>("skills.search", {
        query: trimmed,
        limit: 20,
      });
      if (requestIdRef.current !== reqId) return; // superseded by a newer search / query change
      setResults(toHubRows(hub, res?.results ?? []));
      setSearchedFor(trimmed);
      capture("skill_hub_searched", { query: trimmed, hub });
    } catch (e) {
      if (requestIdRef.current !== reqId) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (requestIdRef.current === reqId) setLoading(false);
    }
  };

  const install = async (slug: string, acknowledgeRisk: boolean) => {
    setInstalling(slug);
    setMsg(null);
    try {
      await callRpc(
        "skills.install",
        // Hermes takes the hub identifier as `source` (one registry, and its own
        // scanner gates the install); OpenClaw takes a registry name plus a slug.
        hub === "hermes"
          ? { source: slug }
          : {
              source: "clawhub",
              slug,
              ...(acknowledgeRisk ? { acknowledgeClawHubRisk: true } : {}),
            },
        INSTALL_TIMEOUT_MS,
      );
      capture("skill_installed", { skill: slug, source: hub });
      setInstalled((prev) => new Set(prev).add(slug));
      setRiskySlug(null);
      setMsg({ slug, text: "Installed — it'll appear in your skills shortly.", kind: "ok" });
      onInstalled();
    } catch (e) {
      const text = e instanceof Error ? e.message : String(e);
      // First failure that looks trust-related → offer an explicit override.
      if (!acknowledgeRisk && TRUST_ERROR.test(text)) {
        setRiskySlug(slug);
      }
      setMsg({ slug, text, kind: "err" });
    } finally {
      setInstalling(null);
    }
  };

  if (!trimmed) return null;

  return (
    <div className="rounded-xl border border-[var(--edge)] bg-cream p-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <p
          className="text-sm text-[var(--midnight-soft)]"
        >
          Not in your list? Search the{" "}
          <strong className="text-[var(--midnight)]">{HUB_LABEL[hub]}</strong>.
        </p>
        <Button
          size="sm"
          onClick={runSearch}
          disabled={loading}
          className="text-xs bg-[var(--midnight)] hover:opacity-90 text-cream shrink-0"
        >
          {loading ? (
            <Loader2 size={16} className="animate-spin" />
          ) : (
            <Search size={16} className="mr-1.5" />
          )}
          Search {HUB_LABEL[hub]} for &ldquo;{trimmed}&rdquo;
        </Button>
      </div>

      {error && (
        <p className="text-xs text-destructive" role="alert">
          Couldn&apos;t reach {HUB_LABEL[hub]}: {error}
        </p>
      )}

      {results && results.length === 0 && (
        <p className="text-xs text-[var(--midnight-muted)]">
          No skills on {HUB_LABEL[hub]} match &ldquo;{searchedFor}&rdquo;.
        </p>
      )}

      {results && results.length > 0 && (
        <div className="grid gap-2">
          {results.map((r) => {
            const isInstalled = installed.has(r.id);
            const isBusy = installing === r.id;
            const showRisk = riskySlug === r.id;
            const rowMsg = msg?.slug === r.id ? msg : null;
            return (
              <div
                key={r.id}
                className="rounded-lg border border-[var(--edge)] bg-[var(--cream)] p-3 space-y-2"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h4
                        className="text-sm font-semibold text-[var(--midnight)] truncate"
                      >
                        {r.title}
                      </h4>
                      <span className="text-caption text-[var(--midnight-muted)] font-mono shrink-0">{r.id}</span>
                      {r.badge && (
                        <span className="text-2xs text-[var(--midnight-muted)] shrink-0">{r.badge}</span>
                      )}
                    </div>
                    {r.summary && (
                      <p className="text-xs text-[var(--midnight-muted)] mt-0.5 line-clamp-2">{r.summary}</p>
                    )}
                  </div>
                  <Button
                    size="sm"
                    variant={isInstalled ? "outline" : "default"}
                    disabled={isBusy || isInstalled}
                    onClick={() => install(r.id, false)}
                    className={
                      isInstalled
                        ? "text-xs border-[var(--edge)] text-ok shrink-0"
                        : "text-xs bg-[var(--midnight)] hover:opacity-90 text-cream shrink-0"
                    }
                  >
                    {isBusy ? (
                      <Loader2 size={16} className="animate-spin" />
                    ) : isInstalled ? (
                      <>
                        <Check size={16} className="mr-1" /> Installed
                      </>
                    ) : (
                      <>
                        <Download size={16} className="mr-1" /> Install
                      </>
                    )}
                  </Button>
                </div>

                {rowMsg && (
                  <p
                    className={
                      rowMsg.kind === "ok"
                        ? "text-xs text-ok"
                        : "text-xs text-destructive"
                    }
                    role={rowMsg.kind === "err" ? "alert" : undefined}
                  >
                    {rowMsg.kind === "err" && (
                      <AlertTriangle size={12} strokeWidth={1.75} className="inline mr-1 -mt-0.5" />
                    )}
                    {rowMsg.text}
                  </p>
                )}

                {showRisk && !isInstalled && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={isBusy}
                    onClick={() => install(r.id, true)}
                    className="text-ui border-warn text-warn hover:bg-cream-deep"
                  >
                    <AlertTriangle size={16} className="mr-1.5" /> Install anyway — I understand the risk
                  </Button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Hermes's hub has no public web index to link to — only the CLI/API this
          panel already calls, so there is nothing to send the owner off to. */}
      {hub === "clawhub" && (
        <a
          href="https://clawhub.ai/skills?sort=downloads&nonSuspicious=true"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 text-xs text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors"
        >
          Browse the full registry on ClawHub
          <ExternalLink size={12} strokeWidth={1.75} />
        </a>
      )}
    </div>
  );
}
