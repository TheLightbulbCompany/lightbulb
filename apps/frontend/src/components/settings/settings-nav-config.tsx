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
// link to a sibling settings route (`href`). Connections lives on its own
// route — /settings/connections — because OAuth providers redirect the
// browser back to it after consent, so it can't be a ?panel= tab.
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
export const ADMIN_ONLY_PANELS = new Set(["usage"]);

// Six intent-ordered groups (design.md §5). The relocated Control config panels
// (Usage, Model & Credits, MCP servers, the ENGINE ROOM trio, Channel Status)
// live here alongside the original Account-group panels.
export const SETTINGS_NAV_SECTIONS: NavSection[] = [
  {
    label: "ACCOUNT",
    items: [
      { id: "profile", name: "Profile", icon: "profile", panel: "profile" },
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
      // LLMPanel verbatim — it already embeds CreditsSection + ModelRoutingCard,
      // so there is NO separate Credits row.
      { id: "providers", name: "Model & Credits", icon: "providers", panel: "providers" },
    ],
  },
  {
    label: "WIRING",
    items: [
      { id: "channels", name: "Channels", icon: "channels", panel: "channels" },
      { id: "connections", name: "Connections", icon: "connections", href: "/settings/connections" },
      // MCP servers (owner-level mcp.servers) — the IntegrationsPanel tab; the
      // OAuth-return *route* above (Connections) is the dedup counterpart (§5).
      { id: "mcp", name: "MCP servers", icon: "mcp", panel: "mcp" },
      { id: "webhooks", name: "Webhooks", icon: "webhooks", panel: "webhooks" },
      { id: "desktop", name: "Desktop", icon: "desktop", panel: "desktop" },
    ],
  },
  {
    label: "ENGINE ROOM",
    items: [
      { id: "skills", name: "Skills", icon: "skills", panel: "skills" },
      { id: "skill-proposals", name: "Skill Proposals", icon: "skill-proposals", panel: "skill-proposals" },
      // Dreams / Memory folded into the Brain panel (its second tab) — no
      // longer a Settings surface.
      // Read-only channel health. `channel-status` is DISTINCT from the WIRING
      // `channels` org-bots tab — they are two surfaces, no key collision.
      { id: "channel-status", name: "Channel Status", icon: "channel-status", panel: "channel-status" },
    ],
  },
  {
    label: "SELLER",
    items: [
      { id: "marketplace", name: "Marketplace", icon: "marketplace", panel: "marketplace" },
    ],
  },
];

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
    case "billing":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="1" y="4" width="22" height="16" rx="2" />
          <line x1="1" y1="10" x2="23" y2="10" />
        </svg>
      );
    case "channels":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      );
    case "webhooks":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M18 16.98h-5.99c-1.1 0-1.95.94-2.48 1.9A4 4 0 0 1 2 17c.01-.7.2-1.4.57-2" />
          <path d="m6 17 3.13-5.78c.53-.97.1-2.18-.5-3.1a4 4 0 1 1 6.89-4.06" />
          <path d="m12 6 3.13 5.73C15.66 12.7 16.9 13 18 13a4 4 0 0 1 0 8" />
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
    case "skill-proposals":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <path d="m9 15 2 2 4-4" />
        </svg>
      );
    case "channel-status":
      return (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="2" />
          <path d="M4.93 19.07a10 10 0 0 1 0-14.14" />
          <path d="M7.76 16.24a6 6 0 0 1 0-8.49" />
          <path d="M16.24 7.76a6 6 0 0 1 0 8.49" />
          <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
        </svg>
      );
    default:
      return null;
  }
}
