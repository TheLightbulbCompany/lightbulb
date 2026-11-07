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

// Settings inside the shell (UX slice 4), in plain groups. Business (what Work › Money showed) joins Plan. Model &
// credits, Skills and MCP are gone (voice pivot, Phase 1 task 9); the Mac app's Local MCP and Desktop stay.
export const SETTINGS_NAV_SECTIONS: NavSection[] = [
  { label: "You", items: [{ id: "profile", name: "Profile", icon: "profile", panel: "profile" }] },
  {
    label: "Workspace",
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
  // What the team is connected to and what agents may do with it, as one list.
  { label: "Connect", items: [{ id: "connections", name: "Connections", icon: "connections", panel: "connections" }] },
  {
    label: "Plan",
    items: [
      { id: "billing", name: "Billing", icon: "billing", panel: "billing" },
      // Admin-gated: hidden from non-admin org members via ADMIN_ONLY_PANELS.
      { id: "usage", name: "Usage", icon: "usage", panel: "usage" },
      { id: "business", name: "Business", icon: "business", panel: "business" },
    ],
  },
  // The Mac app's own settings: shown in the desktop app, or to a workspace with a coding session (SettingsPlace).
  {
    label: "Developer",
    items: [
      // MCP servers on the user's own Mac — desktop-app-only.
      { id: "local-mcp", name: "Local MCP servers", icon: "mcp", panel: "local-mcp" },
      { id: "desktop", name: "Desktop", icon: "desktop", panel: "desktop" },
    ],
  },
];
export const DEVELOPER_PANELS: ReadonlySet<string> = new Set(["local-mcp", "desktop"]);

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
    case "mcp":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="3" y="3" width="7" height="7" />
          <rect x="14" y="3" width="7" height="7" />
          <rect x="14" y="14" width="7" height="7" />
          <rect x="3" y="14" width="7" height="7" />
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
    case "business":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="m22 7-8.5 8.5-5-5L2 17" />
          <path d="M16 7h6v6" />
        </svg>
      );
    default:
      return null;
  }
}
