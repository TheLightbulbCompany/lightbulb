"use client";

// Company → Domain & Site (`?panel=teams-domain-site`).
//
// Upgraded from a shell to a real read when the Stripe Projects catalog turned
// out to carry FOUR registrars (Cloudflare, Spaceship, Squarespace,
// WordPress.com) and every host we would want (Vercel, Netlify, Railway,
// Render, Fly.io). The domain is owner-registered, owner-paid and spend-capped
// through the provisioning rail that already ships — so the old "Route 53 vs
// a Projects registrar" question answered itself, and this tab reads what
// that rail has provisioned.
//
// Composition only: `useProvisioningRail` is the same read the Accounts panel
// makes, so mounting this costs no extra request (SWR dedupes the keys). This
// panel filters it to the providers that are a domain or a host.
//
// The exit is a PROMPT, deliberately. Provisioning is Friday-driven by design
// — she files the request, the owner approves a card with the spend limit,
// the rail does the rest. There is no "buy a domain" button because the whole
// point of the rail is that the agent operates and the owner approves.

import { Globe } from "lucide-react";
import { PanelDataTable, PanelMasthead, PanelNotice, type PanelDataColumn } from "@/components/teams/shell";
import { useProvisioningRail, type ProvisionedResource } from "@/components/settings/ProvisionedResources";

/** Catalog providers whose primary category is domains or hosting, per the
 *  live Projects catalog. Others (analytics, email, AI) belong to other tabs. */
const DOMAIN_OR_HOST = new Set([
  "cloudflare",
  "spaceship",
  "squarespace",
  "wordpress",
  "wordpress.com",
  "vercel",
  "netlify",
  "railway",
  "render",
  "fly",
  "fly.io",
  "wix",
]);

const COLUMNS: PanelDataColumn<ProvisionedResource>[] = [
  { key: "provider", header: "Provider", cell: (r) => r.provider, width: "9rem" },
  { key: "service", header: "Service", cell: (r) => r.service },
  {
    key: "name",
    header: "Resource",
    cell: (r) => r.resource_name || <span className="text-[var(--midnight-muted)]">—</span>,
  },
  {
    key: "status",
    header: "Status",
    width: "8rem",
    // The pointer is only ever written `active` (provisioning_workflow._bind);
    // failures live on the workflow row, not here. Never colour alone
    // (system.md): the word carries it. Unknown renders neutral, never ok.
    cell: (r) =>
      r.status === "active" ? (
        <span className="text-ok">Live</span>
      ) : (
        <span className="text-[var(--midnight-muted)]">{r.status}</span>
      ),
  },
];

export function DomainSitePanel() {
  const { byProvider, error, isLoading } = useProvisioningRail();

  const rows: ProvisionedResource[] = [];
  for (const [provider, list] of byProvider) {
    if (DOMAIN_OR_HOST.has(provider.toLowerCase())) rows.push(...list);
  }

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-4xl mx-auto w-full">
      <PanelMasthead overline="Company" title="Domain & Site" meta="The address your customers type." />

      {error ? (
        <PanelNotice
          state="unavailable"
          icon={Globe}
          headline="We couldn't read your provisioned resources just now."
          supporting="This is a temporary read failure. Check back shortly."
        />
      ) : isLoading && !rows.length ? (
        // Cold cache: both maps empty, no error, nothing arrived yet. Saying
        // "No domain yet" here was a materially false claim on a slow
        // connection for an owner who has one.
        <PanelDataTable rows={[]} columns={COLUMNS} rowKey={(r) => r.resource_id} loading />
      ) : rows.length ? (
        <PanelDataTable rows={rows} columns={COLUMNS} rowKey={(r) => r.resource_id} />
      ) : (
        <PanelNotice
          state="unbound"
          icon={Globe}
          headline="No domain or hosting yet."
          supporting="Your agents can register a domain and set up hosting on your own accounts — you approve the spend, the rail does the rest, and it shows up here."
          prompt="Register a domain for the company and set up hosting for our site."
        />
      )}
    </div>
  );
}
