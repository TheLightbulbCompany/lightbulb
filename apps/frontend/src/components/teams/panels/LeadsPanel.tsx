"use client";

// Growth → Leads (`?panel=teams-leads`).
//
// result.dev: "Say who you want to reach, then work the ones worth working."
// A natural-language box with a prefilled example, four chip suggestions,
// FIND PEOPLE, and a People/Pipeline pair of tabs. This is the People half.
// Pipeline — saved leads worked over time — waits for the Attio write path.
//
// This is the first panel on a tier-2 credential: the owner's pasted Apollo
// key, held backend-side, read by `/company/leads`. The agent's own Apollo
// MCP connection is a different, MCP-scoped grant a backend route cannot use
// — so "connect Apollo (dashboard access)" is the exit, and it is a paste in
// Integrations, not an OAuth dance.
//
// WHAT THE FREE SEARCH RETURNS, SAID OUT LOUD. Apollo's net-new search costs
// 0 credits and returns a first name, a privacy-masked surname ("L."), a
// title, a company, and whether an email exists — not the email itself. The
// table renders exactly that. Un-masking is enrichment, which spends the
// owner's credits and stays behind the agent + an approval; the panel says so
// rather than showing a locked column that implies a paywall we own.

import { useState, type FormEvent } from "react";
import { Users, Mail, MailX } from "lucide-react";
import {
  EditorialButton,
  PanelDataTable,
  PanelMasthead,
  PanelNotice,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useCompanyLeads, type LeadRow } from "@/hooks/useCompanyLeads";
import { handleShellLinkClick } from "@/lib/shellNav";

const INTEGRATIONS = "/chat?panel=integrations";

// Four sentences, the way result.dev seeds its box — enough to show the
// grammar without pretending to know the owner's market.
const CHIPS = [
  "heads of growth at seed-stage devtools startups",
  "B2B SaaS founders in London",
  "VPs of engineering at fintechs",
  "marketing leads at DTC brands",
];

const COLUMNS: PanelDataColumn<LeadRow>[] = [
  {
    key: "who",
    header: "Person",
    cell: (r) => (
      <span>
        {[r.first_name, r.last_name].filter(Boolean).join(" ") || (
          <span className="text-[var(--midnight-muted)]">—</span>
        )}
      </span>
    ),
    width: "12rem",
  },
  { key: "title", header: "Title", cell: (r) => r.title ?? "—" },
  { key: "org", header: "Company", cell: (r) => r.organization ?? "—" },
  {
    key: "where",
    header: "Where",
    cell: (r) => [r.city, r.country].filter(Boolean).join(", ") || "—",
    width: "11rem",
  },
  {
    key: "email",
    header: "Email",
    width: "6rem",
    align: "center",
    // Never colour alone: the icon carries a label. "Available" means Apollo
    // HAS one, not that we show it — enrichment reveals it.
    cell: (r) =>
      r.has_email ? (
        <span className="inline-flex items-center gap-1 text-xs text-[var(--midnight-soft)]">
          <Mail size={12} aria-hidden="true" /> available
        </span>
      ) : (
        <span className="inline-flex items-center gap-1 text-xs text-[var(--midnight-muted)]">
          <MailX size={12} aria-hidden="true" /> none
        </span>
      ),
  },
];

export function LeadsPanel() {
  const { query, search, rows, total, view, hasMore, moreFailed, next } = useCompanyLeads();
  const [draft, setDraft] = useState(CHIPS[0]);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (draft.trim().length >= 2) search(draft);
  }

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-5xl mx-auto w-full">
      <PanelMasthead
        overline="Growth"
        title="Leads"
        meta="Say who you want to reach, then work the ones worth working."
      />

      <form onSubmit={submit} className="flex flex-col gap-2">
        <div className="flex gap-2">
          <input
            aria-label="Who do you want to reach?"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            // The route's max_length; a longer sentence would 422 and read as an outage.
            maxLength={200}
            className="min-w-0 flex-1 rounded-md border border-[var(--edge)] bg-card px-3 py-2 text-ui text-[var(--midnight)] placeholder:text-[var(--midnight-muted)]"
            placeholder="heads of growth at seed-stage devtools startups"
          />
          <EditorialButton type="submit" size="md" disabled={draft.trim().length < 2}>
            Find people
          </EditorialButton>
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {CHIPS.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => {
                setDraft(c);
                search(c);
              }}
              className="text-xs text-[var(--midnight-muted)] underline-offset-2 hover:text-[var(--midnight)] hover:underline"
            >
              ↳ {c}
            </button>
          ))}
        </div>
      </form>

      {view.kind === "idle" && (
        <PanelNotice
          state="zero"
          icon={Users}
          headline="No people yet."
          supporting="Describe who you want to reach and we'll go find them. The search is free; revealing contact details is a separate step your agent asks you before spending on."
        />
      )}
      {view.kind === "unbound" && (
        <PanelNotice
          state="unbound"
          icon={Users}
          headline="Connect Apollo to search for people here."
          supporting="This tab reads with an Apollo API key you paste in Integrations — the “Apollo (dashboard access)” row. Your agents' own Apollo connection can't serve a table."
          prompt="Find me heads of growth at seed-stage devtools startups."
          href={INTEGRATIONS}
          action={{ label: "Open Integrations", onClick: (e) => handleShellLinkClick(e, INTEGRATIONS) }}
        />
      )}
      {view.kind === "forbidden" && (
        <PanelNotice
          state="unavailable"
          icon={Users}
          headline="Only workspace admins can search for leads."
          supporting="Each search is attributed to the workspace on Apollo's side, so it's limited to admins."
        />
      )}
      {view.kind === "key_rejected" && (
        <PanelNotice
          state="locked"
          icon={Users}
          headline="Apollo rejected the API key on file."
          supporting="It may have been revoked or rotated. Paste a fresh one in Integrations — waiting won't fix this."
          href={INTEGRATIONS}
          action={{ label: "Open Integrations", onClick: (e) => handleShellLinkClick(e, INTEGRATIONS) }}
        />
      )}
      {view.kind === "unavailable" && (
        <PanelNotice
          state="unavailable"
          icon={Users}
          headline="We couldn't reach Apollo just now."
          supporting="This is a temporary read failure, not an empty result. Try again shortly."
        />
      )}
      {view.kind === "zero" && (
        <PanelNotice
          state="zero"
          icon={Users}
          headline={`Nobody matched “${query}”.`}
          supporting="Try broader words — a role and an industry usually work better than a company name."
        />
      )}
      {view.kind === "loading" && (
        <PanelDataTable rows={[]} columns={COLUMNS} rowKey={(r) => r.id ?? ""} loading />
      )}
      {view.kind === "rows" && (
        <>
          <p className="text-xs text-[var(--midnight-muted)]">
            {total.toLocaleString()} {total === 1 ? "person" : "people"} for “{query}”. Surnames are
            masked and emails hidden until a lead is enriched — ask Friday to do that for the ones
            worth it.
          </p>
          <PanelDataTable rows={rows} columns={COLUMNS} rowKey={(r) => r.id ?? `${r.first_name}-${r.organization}`} />
          {hasMore && (
            <div className="flex flex-col items-center gap-1">
              <EditorialButton size="sm" variant="secondary" onClick={next}>
                {moreFailed ? "Try again" : "Load more"}
              </EditorialButton>
              {moreFailed && (
                <p className="text-xs text-[var(--midnight-muted)]">
                  Couldn’t load the next page just now — the rows above are still yours.
                </p>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
