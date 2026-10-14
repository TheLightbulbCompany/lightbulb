"use client";

// apps/frontend/src/components/teams/panels/SearchPanel.tsx
//
// Cross-entity company search — surfaces Paperclip's ranked, filtered search
// over issues / comments / documents / artifacts / agents / projects (ported
// shape from upstream Paperclip's pages/Search.tsx + components/search/, MIT ©
// 2025 Paperclip AI). Replaces the old client-side `.includes()` substring
// filter over the ≤10 already-loaded rows with a real server-side ranked query.
//
// The search box accepts inline operator tokens (`status:todo`, `assignee:me`,
// `project:"Acme"`, `is:open`, `updated:>7d`) parsed by searchQueryParser; the
// residual text becomes `q` and the operators become structured filters
// forwarded to the BFF `/teams/search` route (company derived server-side).
//
// v1 renders: debounced query box, scope tabs with per-type counts, a sort
// select, active-filter pills (parsed from the query — edit the text to
// change them), ranked result rows with match highlights, and a zero-results
// hint. Deferred: the bulk `/search/extract` surface (BFF route + admin client
// method ship in this PR but have no UI yet) and operator autocomplete.

import { useEffect, useMemo, useState } from "react";
import { Search as SearchIcon } from "lucide-react";
import { EditorialSelect, PanelMasthead, PanelTabs, PanelToolbar } from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import {
  buildSearchQueryString,
  hasSearchFilters,
  parseSearchQuery,
} from "@/components/teams/shared/lib/searchQueryParser";
import type {
  CompanyAgent,
  CompanySearchResponse,
  CompanySearchScope,
  CompanySearchSort,
  IssueLabel,
  IssueProject,
} from "@/components/teams/shared/types";
import { SearchResultRow } from "@/components/teams/search/SearchResultRow";

const SORT_OPTIONS: { value: CompanySearchSort; label: string }[] = [
  { value: "relevance", label: "Relevance" },
  { value: "updated", label: "Recently updated" },
  { value: "created", label: "Recently created" },
  { value: "priority", label: "Priority" },
];

// Scope tab → the `countsByType` key it badges. `all` sums everything.
const SCOPE_TABS: { value: CompanySearchScope; label: string; countKey?: string }[] = [
  { value: "all", label: "All" },
  { value: "issues", label: "Tasks", countKey: "issue" },
  { value: "comments", label: "Comments", countKey: "comment" },
  { value: "documents", label: "Docs", countKey: "document" },
  { value: "artifacts", label: "Artifacts", countKey: "artifact" },
  { value: "agents", label: "Agents", countKey: "agent" },
  { value: "projects", label: "Projects", countKey: "project" },
];

const DEBOUNCE_MS = 250;

export function SearchPanel() {
  const { read } = useTeamsApi();

  const [input, setInput] = useState("");
  const [debounced, setDebounced] = useState("");
  const [scope, setScope] = useState<CompanySearchScope>("all");
  const [sort, setSort] = useState<CompanySearchSort>("relevance");

  // Debounce the raw box so each keystroke doesn't fire a BFF search.
  useEffect(() => {
    const id = setTimeout(() => setDebounced(input), DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [input]);

  // Parser context: resolve `assignee:`/`project:`/`label:` names → ids. These
  // are light, SWR-cached reads shared with the other Teams panels.
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const { data: projectsData } = read<{ projects: IssueProject[] }>("/projects");
  const { data: labelsData } = read<{ labels: IssueLabel[] }>("/issues/labels");

  const agents = useMemo(() => agentsData?.agents ?? [], [agentsData]);
  const projects = useMemo(() => projectsData?.projects ?? [], [projectsData]);
  const labels = useMemo(() => labelsData?.labels ?? [], [labelsData]);

  const parsed = useMemo(
    () => parseSearchQuery(debounced, { agents, projects, labels }),
    [debounced, agents, projects, labels],
  );

  const agentsById = useMemo(() => {
    const map = new Map<string, string>();
    for (const a of agents) map.set(a.id, a.name);
    return map;
  }, [agents]);

  const active = parsed.query.length > 0 || hasSearchFilters(parsed.filters);

  // Only hit the BFF when there is something to search — an empty box shows the
  // resting hint instead of a firehose "everything" query.
  const searchPath = useMemo(() => {
    if (!active) return null;
    const base = buildSearchQueryString(parsed.query, parsed.filters);
    const params = new URLSearchParams(base);
    params.set("scope", scope);
    params.set("sort", sort);
    return `/search?${params.toString()}`;
  }, [active, parsed.query, parsed.filters, scope, sort]);

  // keepPreviousData holds the last results/counts on screen while a scope or
  // sort switch revalidates, so the list doesn't flash empty between keys.
  const { data, error, isLoading } = read<CompanySearchResponse>(searchPath, {
    keepPreviousData: true,
  });

  const results = data?.results ?? [];
  // countsByType reflects the ACTIVE scope's counts (upstream computes each
  // type's count only when the scope includes it), so a narrowed scope shows 0
  // on the other tabs — matches upstream Paperclip's Search page behavior.
  const counts = data?.countsByType ?? {};
  const totalCount = Object.values(counts).reduce((sum, n) => sum + (n ?? 0), 0);

  const tabs = SCOPE_TABS.map((tab) => ({
    value: tab.value,
    label: tab.label,
    count: tab.countKey ? counts[tab.countKey as keyof typeof counts] ?? 0 : totalCount,
  }));

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-4xl mx-auto w-full">
      <PanelMasthead
        overline="Company"
        title="Search"
        meta={active && data ? `${totalCount} ${totalCount === 1 ? "match" : "matches"}` : undefined}
      />

      <PanelToolbar
        searchValue={input}
        onSearchChange={setInput}
        searchPlaceholder="Search tasks, docs, artifacts…  try status:todo or assignee:me"
        searchAriaLabel="Search company"
        filters={
          <EditorialSelect
            options={SORT_OPTIONS}
            value={sort}
            onValueChange={(v) => setSort(v as CompanySearchSort)}
            className="h-8 w-[168px] text-ui"
          />
        }
      />

      {parsed.pills.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1.5" aria-label="Active filters">
          {parsed.pills.map((pill, i) => (
            <span
              key={`${pill.key}-${pill.value}-${i}`}
              className="rounded-full border px-2 py-0.5 text-caption font-mono"
              style={{ borderColor: "var(--edge)", color: "var(--midnight-soft)" }}
            >
              {pill.label}
            </span>
          ))}
        </div>
      ) : null}

      {active ? (
        <PanelTabs
          tabs={tabs}
          value={scope}
          onValueChange={(v) => setScope(v as CompanySearchScope)}
        />
      ) : null}

      <section aria-label="Search results" className="min-w-0">
        {!active ? (
          <div className="flex flex-col items-center gap-2 py-12 text-center">
            <SearchIcon size={20} style={{ color: "var(--midnight-muted)" }} />
            <p className="font-editorial-serif text-base italic" style={{ color: "var(--midnight-muted)" }}>
              Search across tasks, comments, docs, artifacts, agents, and projects.
            </p>
            <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
              Use operators like{" "}
              <code className="font-mono">status:todo</code>,{" "}
              <code className="font-mono">assignee:me</code>,{" "}
              <code className="font-mono">is:open</code>, or{" "}
              <code className="font-mono">updated:&gt;7d</code>.
            </p>
          </div>
        ) : isLoading && results.length === 0 ? (
          <p className="font-editorial-serif text-base italic" style={{ color: "var(--midnight-muted)" }}>
            Searching…
          </p>
        ) : error ? (
          <p
            role="alert"
            className="font-editorial-serif text-base italic"
            style={{ color: "var(--warn)" }}
          >
            Search failed. Try again.
          </p>
        ) : results.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-12 text-center">
            <p className="font-editorial-serif text-base italic" style={{ color: "var(--midnight-muted)" }}>
              No matches for this search.
            </p>
            {data?.zeroResults && data.zeroResults.unfilteredTotal > 0 ? (
              <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
                {data.zeroResults.unfilteredTotal} result
                {data.zeroResults.unfilteredTotal === 1 ? "" : "s"} match the text without the
                active filters — loosen a filter to see them.
              </p>
            ) : (
              <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
                Try a different term or fewer filters.
              </p>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {results.map((result) => (
              <SearchResultRow key={result.id} result={result} agentsById={agentsById} />
            ))}
            {data?.hasMore ? (
              <p className="pt-2 text-center text-caption" style={{ color: "var(--midnight-muted)" }}>
                Showing the top {results.length} matches — refine your search to narrow further.
              </p>
            ) : null}
          </div>
        )}
      </section>
    </div>
  );
}
