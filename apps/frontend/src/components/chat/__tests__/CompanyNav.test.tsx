import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// CompanyNav reads /company/stripe/status (shared SWR key with the Stripe
// card) to learn whether the provisioning tabs are offered. Default it ON so
// the IA tests below see the full set; the off-state test flips it.
const stripeStatus = vi.hoisted(() => ({ data: { provisioning_enabled: true } as { provisioning_enabled: boolean } | undefined }));
vi.mock("@/components/settings/CompanyStripeSection", () => ({
  useCompanyStripeStatus: () => stripeStatus,
}));

import { CompanyNav, COMPANY_SECTIONS } from "../CompanyNav";
import { visibleCompanySections } from "../companySections";
import { resolvePanelAlias, describePanel } from "../../workspace/panelRegistry";

// The live nav set, in the spec §5.1 IA. "Issues" was renamed to "Tasks" and
// Members removed (#1022), Costs cut (#1003), Skills retired for in-app
// ClawHub search (#1175); Artifacts (#1307, now "Library") and Search (#1312)
// were added; "Dashboard" became "Home" with the IA shell. The consolidation
// (spec 2026-09-06) took it to NINE: Org chart became a view of the Agents
// panel and Brain came off the rail — Search stays, overriding the spec
// (owner ruling 2026-09-08).
const ALL_LABELS = [
  "Home",
  "Search",
  "Inbox",
  "Money",
  "Tasks",
  "Automations",
  "Leads",
  "Library",
  "Accounts",
];

/** Shells whose capability is not live: registered, reachable by URL, and
 *  deliberately NOT offered in the rail or the palette. */
const COMING_SOON_LABELS = [
  "Support",
  "Publish",
  "SEO & AEO",
  "Ads",
  "Identity",
  "Compliance",
  "Contracts",
];

describe("CompanyNav", () => {
  beforeEach(() => {
    stripeStatus.data = { provisioning_enabled: true };
  });

  it("hides Domain & Site while the rail is off (and while status is still loading) but keeps Accounts, which also holds the guardrails and revoke-all", () => {
    stripeStatus.data = { provisioning_enabled: false };
    const { unmount } = render(<CompanyNav />);
    expect(screen.getByText("Accounts")).toBeInTheDocument();
    expect(screen.queryByText("Domain & Site")).toBeNull();
    unmount();

    stripeStatus.data = undefined;
    render(<CompanyNav />);
    expect(screen.getByText("Accounts")).toBeInTheDocument();
    expect(screen.queryByText("Domain & Site")).toBeNull();
  });

  it("drops the shells always, and Domain & Site only while the rail is off", () => {
    const on = visibleCompanySections({ provisioningEnabled: true });
    const off = visibleCompanySections({ provisioningEnabled: false });
    const flat = (s: typeof on) => s.flatMap((sec) => sec.items.map((i) => i.panel));
    const all = COMPANY_SECTIONS.flatMap((sec) => sec.items.map((i) => i.panel));

    const shells = [
      "teams-support",
      "teams-publish",
      "teams-seo",
      "teams-ads",
      "teams-identity",
      "teams-compliance",
      "teams-contracts",
    ];
    // Hidden in BOTH modes — a shell is not a provisioning question.
    for (const panel of shells) {
      expect(flat(on)).not.toContain(panel);
      expect(flat(off)).not.toContain(panel);
    }
    // Compared as sets: teams-domain-site sits mid-list in the Company
    // section, so declaration order is not the drop order.
    expect(all.filter((p) => !flat(on).includes(p)).sort()).toEqual([...shells].sort());
    expect(all.filter((p) => !flat(off).includes(p)).sort()).toEqual(
      [...shells, "teams-domain-site"].sort(),
    );
    // No section is emptied, so the section order is intact.
    expect(off.map((s) => s.label)).toEqual(on.map((s) => s.label));
  });

  it("keeps every shell registered so an old link still opens it", () => {
    // The rail stops offering them; the panel registry must not stop resolving
    // them, or a saved link or an agent-built URL breaks.
    const all = COMPANY_SECTIONS.flatMap((sec) => sec.items.map((i) => i.panel));
    for (const panel of ["teams-support", "teams-publish", "teams-seo", "teams-ads", "teams-identity", "teams-compliance", "teams-contracts"]) {
      expect(all).toContain(panel);
    }
  });

  it("does not offer a shell in the rail", () => {
    render(<CompanyNav />);
    for (const label of COMING_SOON_LABELS) {
      expect(screen.queryByText(label)).toBeNull();
    }
  });

  it("renders every company panel", () => {
    render(<CompanyNav />);
    for (const label of ALL_LABELS) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });
  it("declares the spec §5.1 section order: top block, Money, Work, Growth, Knowledge, Company", () => {
    expect(COMPANY_SECTIONS.map((s) => s.label)).toEqual([
      null,
      null, // Money — a one-item group carries no caption
      "Work",
      "Growth",
      "Knowledge",
      "Company",
    ]);
  });
  it("files a single, uncaptioned Money item and opens teams-money on click", () => {
    // Payments/Products/Customers/Spend are absorbed into the one ledger page
    // (spec §3) — the sidebar carries one item, not four peer tabs, and a
    // one-item group carries no caption (the item IS the caption).
    const money = COMPANY_SECTIONS.find((s) => s.items.some((i) => i.panel === "teams-money"));
    expect(money?.label).toBeNull();
    expect(money?.items.map((i) => i.label)).toEqual(["Money"]);
    const onPanelChange = vi.fn();
    render(<CompanyNav onPanelChange={onPanelChange} />);
    fireEvent.click(screen.getByText("Money"));
    expect(onPanelChange).toHaveBeenCalledWith("teams-money");
  });
  it('puts "Home" first', () => {
    expect(COMPANY_SECTIONS[0].items[0].label).toBe("Home");
  });
  it('files "Library" under Knowledge and opens teams-artifacts on click', () => {
    const knowledge = COMPANY_SECTIONS.find((s) => s.label === "Knowledge");
    expect(knowledge?.items.map((i) => i.label)).toEqual(["Library"]);
    const onPanelChange = vi.fn();
    render(<CompanyNav onPanelChange={onPanelChange} />);
    fireEvent.click(screen.getByText("Library"));
    expect(onPanelChange).toHaveBeenCalledWith("teams-artifacts");
  });
  it("offers exactly nine items, in the consolidated order", () => {
    // The rail cut's whole point (spec §3), so it is asserted as an ordered
    // list, not a count: 22 items became 9. Provisioning off, since Domain &
    // Site is a capability question rather than an IA one.
    const labels = visibleCompanySections({ provisioningEnabled: false }).flatMap((s) =>
      s.items.map((i) => i.label),
    );
    expect(labels).toEqual(ALL_LABELS);
    expect(labels).toHaveLength(9);
  });

  it("stops offering Brain, whose key is gone entirely", () => {
    // Owner ruling 2026-09-08 took it off the rail; the memory wiki it read is
    // gone outright — there is no memory wiki on this runtime.
    render(<CompanyNav />);
    expect(screen.queryByText("Brain")).toBeNull();
    const all = COMPANY_SECTIONS.flatMap((sec) => sec.items.map((i) => i.panel));
    expect(all).not.toContain("teams-brain");
    expect(describePanel("teams-brain", {})).toBeNull();
  });

  it("stops offering Org chart, which still resolves onto the Agents panel", () => {
    // Absorbed as the Agents panel's chart view (owner ruling 2026-09-08). The
    // rail drops it; the key must keep resolving, or a saved link, a bookmark
    // or an agent-built URL breaks.
    render(<CompanyNav />);
    expect(screen.queryByText("Org chart")).toBeNull();
    const all = COMPANY_SECTIONS.flatMap((sec) => sec.items.map((i) => i.panel));
    expect(all).not.toContain("teams-org-chart");
    expect(resolvePanelAlias("teams-org-chart")).toBe("teams-agents");
    expect(describePanel("teams-org-chart", {})).toMatchObject({
      panelId: "teams-agents",
      params: { teamsKey: "agents", view: "chart" },
    });
  });

  it("has no dead ends — every item's panel key resolves in the registry", () => {
    for (const section of COMPANY_SECTIONS) {
      for (const item of section.items) {
        expect(
          describePanel(resolvePanelAlias(item.panel), {}),
          `panel key "${item.panel}" does not resolve`,
        ).not.toBeNull();
      }
    }
  });
  it("emits the teams- panel key on click", () => {
    const onPanelChange = vi.fn();
    render(<CompanyNav onPanelChange={onPanelChange} />);
    // Label is "Tasks"; the panel key stayed `teams-issues`.
    fireEvent.click(screen.getByText("Tasks"));
    expect(onPanelChange).toHaveBeenCalledWith("teams-issues");
    fireEvent.click(screen.getByText("Automations"));
    expect(onPanelChange).toHaveBeenCalledWith("teams-routines");
  });
  it("marks the active item with the midnight accent", () => {
    render(<CompanyNav activePanel="teams-routines" />);
    const btn = screen.getByText("Automations").closest("button");
    expect(btn?.className).toMatch(/--midnight/);
  });
  // The desktop shell paints this nav while the provisioning stepper has the
  // dockview host unmounted, and `openPanel` early-returns when the dock api is
  // null — so every one of these clicks was a silent no-op with the button
  // rendered fully enabled. An inert-but-enabled control is the same class of
  // defect as a false progress bar: the UI asserting a capability it lacks.
  it("renders items as unavailable, with a reason, when disabled", () => {
    const onPanelChange = vi.fn();
    render(
      <CompanyNav
        disabled
        disabledTitle="Available once your workspace is ready"
        onPanelChange={onPanelChange}
      />,
    );
    for (const label of ALL_LABELS) {
      const btn = screen.getByText(label).closest("button");
      expect(btn, `"${label}" should be disabled`).toBeDisabled();
      expect(btn).toHaveAttribute("title", "Available once your workspace is ready");
    }
    fireEvent.click(screen.getByText("Tasks"));
    expect(onPanelChange).not.toHaveBeenCalled();
  });

  it("is enabled and untitled by default (mobile's ChatLayout routes it somewhere live)", () => {
    render(<CompanyNav onPanelChange={vi.fn()} />);
    const btn = screen.getByText("Tasks").closest("button");
    expect(btn).toBeEnabled();
    expect(btn).not.toHaveAttribute("title");
  });

  it("owns no scroll region — the shared .sidebar-scroll wrapper owns the single scroll", () => {
    // Inverted deliberately in #1002: the nav used to carry its own
    // `min-h-0 overflow-y-auto`, which nested a second scroll inside
    // `.sidebar-scroll` and collapsed the roster's height. Re-adding any
    // overflow/scroll class here reintroduces that bug.
    render(<CompanyNav />);
    const nav = screen.getByRole("navigation", { name: "Company" });
    expect(nav.className).not.toMatch(/overflow-/);
    expect(nav.className).not.toMatch(/\boverscroll-/);
  });
});
