"use client";

import React from "react";

// =============================================================================
// Settings nav config — the single source of truth for the Settings sidebar.
//
// Read by /settings (page.tsx) and SettingsNav. Keep the section consts, the
// NavItem type, and NavIcon here only — page.tsx re-imports them.
// =============================================================================

// A nav item is either an in-page tab (sets activePanel) or an outbound
// link to a sibling settings route (`href`). NO row uses `href` today; the
// field and SettingsNav's matching link mode are the generic escape hatch for
// the next row that needs to be a route.
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
export const ADMIN_ONLY_PANELS = new Set(["usage", "members", "approvals"]);

// The one Settings page (product simplification, 2026-10-14): Control's panels,
// the Teams Settings tab and the Accounts tab all live here, in six groups.
export const SETTINGS_NAV_SECTIONS: NavSection[] = [
  {
    label: "YOU",
    items: [{ id: "profile", name: "Profile", icon: "profile", panel: "profile" }],
  },
  {
    label: "WORKSPACE",
    items: [
      // The company's name and description (was the Teams Settings tab).
      { id: "workspace", name: "Workspace", icon: "workspace", panel: "workspace" },
      // The roster and the join link. Admin-gated via ADMIN_ONLY_PANELS.
      { id: "members", name: "Members", icon: "members", panel: "members" },
      // How agents ask (Ask / Auto / Never ask), the Always-allow rules, the kill switch. Admin-gated: the
      // policy routes are admin-only (company_policy._owner).
      { id: "approvals", name: "Approvals", icon: "approvals", panel: "approvals" },
    ],
  },
  {
    label: "CONNECTIONS",
    items: [
      // What the team is connected to and what agents may do with it: the
      // dock's Integrations, Connections and Accounts tabs, as one list.
      { id: "connections", name: "Connections", icon: "connections", panel: "connections" },
    ],
  },
  {
    label: "PLAN AND USAGE",
    items: [
      { id: "billing", name: "Billing", icon: "billing", panel: "billing" },
      // Admin-gated: hidden from non-admin org members via ADMIN_ONLY_PANELS.
      { id: "usage", name: "Usage", icon: "usage", panel: "usage" },
      // Paperclip-backed spend vs monthly budget. Readable by every member;
      // only admins see the set-budget control, so it is NOT in ADMIN_ONLY_PANELS.
      { id: "budgets", name: "Budgets", icon: "usage", panel: "budgets" },
    ],
  },
  {
    label: "MODEL",
    items: [
      // LLMPanel verbatim — it already embeds CreditsSection, so there is NO
      // separate Credits row.
      { id: "providers", name: "Model & Credits", icon: "providers", panel: "providers" },
    ],
  },
  {
    label: "ADVANCED",
    items: [
      { id: "skills", name: "Skills", icon: "skills", panel: "skills" },
      // MCP servers (owner-level mcp.servers).
      { id: "mcp", name: "MCP servers", icon: "mcp", panel: "mcp" },
      // MCP servers on the user's own Mac — desktop-app-only.
      { id: "local-mcp", name: "Local MCP servers", icon: "mcp", panel: "local-mcp" },
      { id: "desktop", name: "Desktop", icon: "desktop", panel: "desktop" },
      { id: "marketplace", name: "Marketplace", icon: "marketplace", panel: "marketplace" },
    ],
  },
];

// Super-agent (UX slice 4): the same Settings inside the shell, in plain groups. Business (what
// Work › Money showed) joins Plan; Model's single row folds into Plan; Budgets and Marketplace are not shown.
const ROW = (id: string) => SETTINGS_NAV_SECTIONS.flatMap((s) => s.items).find((item) => item.id === id)!;
export const SUPER_AGENT_SETTINGS_NAV_SECTIONS: NavSection[] = [
  { label: "You", items: [ROW("profile")] },
  { label: "Workspace", items: [ROW("workspace"), ROW("members"), ROW("approvals")] },
  { label: "Connect", items: [ROW("connections")] },
  {
    label: "Plan",
    items: [ROW("billing"), ROW("usage"),
      { id: "business", name: "Business", icon: "business", panel: "business" },
      { ...ROW("providers"), name: "Model & credits" }],
  },
  // Shown only to a workspace that has used them (SettingsPlace's useShowsDeveloper): a coding session, the desktop
  // app, or an MCP server of its own (W13-10). Session notifications live under Desktop here, not in Profile.
  { label: "Developer", items: [ROW("skills"), ROW("mcp"), ROW("local-mcp"), ROW("desktop")] },
];
export const DEVELOPER_PANELS: ReadonlySet<string> = new Set(["skills", "mcp", "local-mcp", "desktop"]);

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
    case "workspace":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="4" y="2" width="16" height="20" rx="2" />
          <path d="M9 22v-4h6v4" />
          <path d="M8 6h.01M16 6h.01M12 6h.01M12 10h.01M12 14h.01M16 10h.01M16 14h.01M8 10h.01M8 14h.01" />
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
    case "approvals":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          <path d="m9 12 2 2 4-4" />
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
    case "business":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="m22 7-8.5 8.5-5-5L2 17" />
          <path d="M16 7h6v6" />
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
