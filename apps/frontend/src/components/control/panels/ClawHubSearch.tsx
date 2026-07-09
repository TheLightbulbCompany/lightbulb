"use client";

// In-app ClawHub registry search + one-click install. Rides the same gateway
// RPCs the container already exposes (`skills.search` → ClawHub, `skills.install`
// with `{source:"clawhub"}`), so no backend proxy is involved — the whole flow
// goes over the operator WebSocket like the rest of the Skill Store.

import { useEffect, useState } from "react";
import { Loader2, Download, Search, AlertTriangle, ExternalLink, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { capture } from "@/lib/analytics";

// Mirrors OpenClaw ClawHubSkillSearchResult (src/infra/clawhub.ts).
interface ClawHubResult {
  slug: string;
  displayName: string;
  summary?: string;
  version?: string;
}

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

export function ClawHubSearch({
  query,
  callRpc,
  onInstalled,
}: {
  query: string;
  callRpc: CallRpc;
  onInstalled: () => void;
}) {
  const [results, setResults] = useState<ClawHubResult[] | null>(null);
  const [searchedFor, setSearchedFor] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [installing, setInstalling] = useState<string | null>(null);
  const [installed, setInstalled] = useState<Set<string>>(new Set());
  const [riskySlug, setRiskySlug] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ slug: string; text: string; kind: "ok" | "err" } | null>(null);

  const trimmed = query.trim();

  // Results belong to the query they were fetched for. When the Skill Store
  // filter changes, drop the previous ClawHub results (and their transient
  // install state) so stale rows can't be shown — or installed — under a
  // prompt they no longer match.
  useEffect(() => {
    setResults(null);
    setSearchedFor("");
    setError(null);
    setRiskySlug(null);
    setMsg(null);
  }, [query]);

  const runSearch = async () => {
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    setResults(null);
    setRiskySlug(null);
    setMsg(null);
    try {
      const res = await callRpc<{ results?: ClawHubResult[] }>("skills.search", {
        query: trimmed,
        limit: 20,
      });
      setResults(res?.results ?? []);
      setSearchedFor(trimmed);
      capture("clawhub_searched", { query: trimmed });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const install = async (slug: string, acknowledgeRisk: boolean) => {
    setInstalling(slug);
    setMsg(null);
    try {
      await callRpc(
        "skills.install",
        {
          source: "clawhub",
          slug,
          ...(acknowledgeRisk ? { acknowledgeClawHubRisk: true } : {}),
        },
        INSTALL_TIMEOUT_MS,
      );
      capture("skill_installed", { skill: slug, source: "clawhub" });
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
    <div className="rounded-xl border border-[#e0dbd0] bg-white p-4 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <p
          className="text-sm text-[#5a5549]"
          style={{ fontFamily: "'DM Sans', sans-serif" }}
        >
          Not in your list? Search the{" "}
          <strong className="text-[#1a1a1a]">ClawHub</strong> registry.
        </p>
        <Button
          size="sm"
          onClick={runSearch}
          disabled={loading}
          className="text-xs bg-[#06402B] hover:bg-[#052f20] text-white shrink-0"
          style={{ fontFamily: "'DM Sans', sans-serif" }}
        >
          {loading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Search className="h-3.5 w-3.5 mr-1.5" />
          )}
          Search ClawHub for &ldquo;{trimmed}&rdquo;
        </Button>
      </div>

      {error && (
        <p className="text-xs text-destructive" role="alert">
          Couldn&apos;t reach ClawHub: {error}
        </p>
      )}

      {results && results.length === 0 && (
        <p className="text-xs text-[#8a8578]" style={{ fontFamily: "'DM Sans', sans-serif" }}>
          No skills on ClawHub match &ldquo;{searchedFor}&rdquo;.
        </p>
      )}

      {results && results.length > 0 && (
        <div className="grid gap-2">
          {results.map((r) => {
            const isInstalled = installed.has(r.slug);
            const isBusy = installing === r.slug;
            const showRisk = riskySlug === r.slug;
            const rowMsg = msg?.slug === r.slug ? msg : null;
            return (
              <div
                key={r.slug}
                className="rounded-lg border border-[#e0dbd0] bg-[#faf7f2] p-3 space-y-2"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h4
                        className="text-sm font-semibold text-[#1a1a1a] truncate"
                        style={{ fontFamily: "'DM Sans', sans-serif" }}
                      >
                        {r.displayName || r.slug}
                      </h4>
                      <span className="text-[11px] text-[#8a8578] font-mono shrink-0">{r.slug}</span>
                      {r.version && (
                        <span className="text-[10px] text-[#b5ae9e] shrink-0">v{r.version}</span>
                      )}
                    </div>
                    {r.summary && (
                      <p className="text-xs text-[#8a8578] mt-0.5 line-clamp-2">{r.summary}</p>
                    )}
                  </div>
                  <Button
                    size="sm"
                    variant={isInstalled ? "outline" : "default"}
                    disabled={isBusy || isInstalled}
                    onClick={() => install(r.slug, false)}
                    className={
                      isInstalled
                        ? "text-xs border-[#e0dbd0] text-[#2d8a4e] shrink-0"
                        : "text-xs bg-[#06402B] hover:bg-[#052f20] text-white shrink-0"
                    }
                    style={{ fontFamily: "'DM Sans', sans-serif" }}
                  >
                    {isBusy ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : isInstalled ? (
                      <>
                        <Check className="h-3.5 w-3.5 mr-1" /> Installed
                      </>
                    ) : (
                      <>
                        <Download className="h-3.5 w-3.5 mr-1" /> Install
                      </>
                    )}
                  </Button>
                </div>

                {rowMsg && (
                  <p
                    className={
                      rowMsg.kind === "ok"
                        ? "text-xs text-[#2d8a4e]"
                        : "text-xs text-destructive"
                    }
                    role={rowMsg.kind === "err" ? "alert" : undefined}
                  >
                    {rowMsg.kind === "err" && (
                      <AlertTriangle className="h-3 w-3 inline mr-1 -mt-0.5" />
                    )}
                    {rowMsg.text}
                  </p>
                )}

                {showRisk && !isInstalled && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={isBusy}
                    onClick={() => install(r.slug, true)}
                    className="text-xs border-[#d9a441] text-[#a9791f] hover:bg-[#fbf3e2]"
                    style={{ fontFamily: "'DM Sans', sans-serif" }}
                  >
                    <AlertTriangle className="h-3.5 w-3.5 mr-1.5" /> Install anyway — I understand the risk
                  </Button>
                )}
              </div>
            );
          })}
        </div>
      )}

      <a
        href="https://clawhub.ai/skills?sort=downloads&nonSuspicious=true"
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1.5 text-xs text-[#8a8578] hover:text-[#06402B] transition-colors"
        style={{ fontFamily: "'DM Sans', sans-serif" }}
      >
        Browse the full registry on ClawHub
        <ExternalLink className="h-3 w-3" />
      </a>
    </div>
  );
}
