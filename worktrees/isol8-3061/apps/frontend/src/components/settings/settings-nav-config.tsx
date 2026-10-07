"use client";

import React from "react";

// =============================================================================
// Settings nav config — the single source of truth for the Settings sidebar.
//
// Extracted from settings/page.tsx so BOTH /settings (page.tsx, interactive
// tabs) and /settings/connections (connections/page.tsx, link mode) render the
// identical 6-group nav from one definition. Keep the section consts, the
// NavItem type, and NavIcon here only — page.tsx re-imports them.
// =============================================================================

// A nav item is either an in-page tab (sets activePanel) or an outbound
// link to a sibling settings route (`href`). NO row uses `href` today:
// Connections was the only one, and it moved out of Settings entirely onto the
// workspace rail (see RELOCATED_PANEL_HREFS below). The field and SettingsNav's
// matching link mode are left in place rather than torn out with it — they are
// the generic escape hatch for the next row that needs to be a route.
export type NavItem = {
  id: string;
  name: string;
  icon: string;
  panel?: string;
  href?: string;
};

export type NavSection = { label: string; items: NavItem[] };

// Panels hidden from non-admin org members. Lives with the nav config because
// the Settings nav is the surface that filters on it (the retired control
// sidebar was the original home).
//
// `members` (the roster + the workspace join link) belongs here and NOT behind
// a stricter org-only gate: `useIsWorkspaceAdmin` treats a personal owner as an
// admin, so a personal owner keeps the row and the panel explains there is
// nobody to invite yet, with a link to create a shared workspace. Hiding it
// from them instead would leave "how do I add someone?" with no answer anywhere
// in the app — which is the gap this panel exists to close. The admin gate is
// load-bearing for org members, though: a join link is a credential, so only an
// admin may see or mint one (the panel and the backend both re-check).
export const ADMIN_ONLY_PANELS = new Set(["usage", "members"]);

// Six intent-ordered groups (design.md §5). The relocated Control config panels
// (Usage, Model & Credits, MCP servers, the ENGINE ROOM rows) live here
// alongside the original Account-group panels.
export const SETTINGS_NAV_SECTIONS: NavSection[] = [
  {
    label: "ACCOUNT",
    items: [
      { id: "profile", name: "Profile", icon: "profile", panel: "profile" },
      // Workspace membership — the roster and the join link. Admin-gated via
      // ADMIN_ONLY_PANELS, same as Usage.
      { id: "members", name: "Members", icon: "members", panel: "members" },
    ],
  },
  {
    label: "BILLING & USAGE",
    items: [
      { id: "billing", name: "Billing", icon: "billing", panel: "billing" },
      // Admin-gated: hidden from non-admin org members via ADMIN_ONLY_PANELS.
      { id: "usage", name: "Usage", icon: "usage", panel: "usage" },
    ],
  },
  {
    label: "MODEL & PROVIDERS",
    items: [
      // LLMPanel verbatim — it already embeds CreditsSection, so there is NO
      // separate Credits row.
      { id: "providers", name: "Model & Credits", icon: "providers", panel: "providers" },
    ],
  },
  {
    label: "WIRING",
    items: [
      // MCP servers (owner-level mcp.servers) — the IntegrationsPanel tab.
      // Connections, its dedup counterpart, is no longer a Settings row at all:
      // it lives on the workspace rail under Browser and Files.
      { id: "mcp", name: "MCP servers", icon: "mcp", panel: "mcp" },
      { id: "desktop", name: "Desktop", icon: "desktop", panel: "desktop" },
      // MCP servers on the user's own Mac — desktop-app-only, so it sits with
      // the other Desktop row rather than with the container-side `mcp` tab.
      { id: "local-mcp", name: "Local MCP servers", icon: "mcp", panel: "local-mcp" },
    ],
  },
  {
    label: "ENGINE ROOM",
    items: [
      { id: "skills", name: "Skills", icon: "skills", panel: "skills" },
    ],
  },
  {
    label: "SELLER",
    items: [
      { id: "marketplace", name: "Marketplace", icon: "marketplace", panel: "marketplace" },
    ],
  },
];

// Panels that used to be Settings tabs/routes and now live elsewhere in the
// app. `/settings?panel=<id>` forwards here instead of silently falling back to
// Profile, so links and bookmarks minted while the panel lived in Settings
// still land on it.
export const RELOCATED_PANEL_HREFS: Readonly<Record<string, string>> = {
  connections: "/chat?panel=connections",
};

// =============================================================================
// Icons
// =============================================================================

export function NavIcon({ icon }: { icon: string }) {
  switch (icon) {
    case "profile":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
          <circle cx="12" cy="7" r="4" />
        </svg>
      );
    case "members":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
          <circle cx="9" cy="7" r="4" />
          <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
          <path d="M16 3.13a4 4 0 0 1 0 7.75" />
        </svg>
      );
    case "billing":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="1" y="4" width="22" height="16" rx="2" />
          <line x1="1" y1="10" x2="23" y2="10" />
        </svg>
      );
    case "desktop":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="2" y="3" width="20" height="18" rx="2" />
          <path d="m7 8 3 3-3 3" />
          <path d="M13 15h4" />
        </svg>
      );
    case "marketplace":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3 9l1-5h16l1 5" />
          <path d="M3 9v11a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1V9" />
          <path d="M8 13h8" />
        </svg>
      );
    case "connections":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12 22v-5" />
          <path d="M9 8V2" />
          <path d="M15 8V2" />
          <path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z" />
        </svg>
      );
    case "usage":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <line x1="18" y1="20" x2="18" y2="10" />
          <line x1="12" y1="20" x2="12" y2="4" />
          <line x1="6" y1="20" x2="6" y2="14" />
        </svg>
      );
    case "providers":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
        </svg>
      );
    case "mcp":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="3" y="3" width="7" height="7" />
          <rect x="14" y="3" width="7" height="7" />
          <rect x="14" y="14" width="7" height="7" />
          <rect x="3" y="14" width="7" height="7" />
        </svg>
      );
    case "skills":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
        </svg>
      );
    default:
      return null;
  }
}
