"use client";

import React from "react";
import Link from "next/link";

import { cn } from "@/lib/utils";
import {
  ADMIN_ONLY_PANELS,
  NavIcon,
  type NavItem,
  type NavSection,
} from "@/components/settings/settings-nav-config";

// =============================================================================
// SettingsNav — the shared <nav className="settings-nav"> sidebar.
//
// Two modes. /settings is the only caller today, in "tabs" mode; "links" mode
// existed for the /settings/connections page, which became a redirector when
// Connections moved to the workspace rail. Both are kept so a future sibling
// route gets the full nav instead of a minimal back-link stub.
//
//   mode="tabs" (/settings) — panel items are interactive <li role="tab">
//     with the roving-tabindex keyboard nav; the caller owns activePanel +
//     the keydown handler. Outbound href items render as plain links (never
//     active here). Byte-identical to the pre-extraction render.
//
//   mode="links" (a sibling route) — panel items render as
//     <Link href="/settings?panel={id}"> so they navigate back into the tabbed
//     page on the right pane, and the active href row is marked active. No
//     roving-tabindex (these are real links).
// =============================================================================

type SettingsNavProps = {
  sections: NavSection[];
  isAdmin: boolean;
} & (
  | {
      mode: "tabs";
      activePanel: string;
      onSelectPanel: (panel: string) => void;
      onPanelKeyDown: (
        e: React.KeyboardEvent,
        item: NavItem & { panel: string },
      ) => void;
    }
  | {
      mode: "links";
      // The href of the row that should render active (e.g. the Connections
      // route the user is currently on).
      activeHref: string;
    }
);

export function SettingsNav(props: SettingsNavProps) {
  const { sections, isAdmin } = props;

  return (
    <nav className="settings-nav" aria-label="Settings navigation">
      {sections.map((section) => (
        <div key={section.label} className="settings-nav-section">
          <div className="settings-nav-section-label">{section.label}</div>
          <ul className="settings-nav-items" role={props.mode === "tabs" ? "tablist" : undefined}>
            {section.items
              .filter(
                (item) =>
                  isAdmin ||
                  item.panel === undefined ||
                  !ADMIN_ONLY_PANELS.has(item.panel),
              )
              .map((item) => (
                <SettingsNavRow key={item.id} item={item} {...props} />
              ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}

function SettingsNavRow({ item, ...props }: { item: NavItem } & SettingsNavProps) {
  // Outbound link to a sibling settings route (Connections).
  if (item.href) {
    const isActive = props.mode === "links" && props.activeHref === item.href;
    if (props.mode === "links" && isActive) {
      // The route the user is currently on — render active, not a link, so it
      // mirrors how the active in-page tab reads (no self-navigation).
      return (
        <li className="settings-nav-item active" aria-current="page">
          <NavIcon icon={item.icon} />
          {item.name}
        </li>
      );
    }
    // Plain outbound link — byte-identical to the pre-extraction render on
    // /settings, and the (inactive) Connections row on the connections route.
    return (
      <li role="none">
        <Link href={item.href} className="settings-nav-item" style={{ color: "inherit", textDecoration: "none" }}>
          <NavIcon icon={item.icon} />
          {item.name}
        </Link>
      </li>
    );
  }

  // In-page panel item.
  if (props.mode === "links") {
    // On the connections route, panel items can't set state on the tabbed
    // page — render them as links back into /settings?panel={id}.
    return (
      <li role="none">
        <Link
          href={`/settings?panel=${item.panel}`}
          className="settings-nav-item"
          style={{ color: "inherit", textDecoration: "none" }}
        >
          <NavIcon icon={item.icon} />
          {item.name}
        </Link>
      </li>
    );
  }

  // mode === "tabs": interactive tab with roving-tabindex keyboard nav.
  const active = props.activePanel === item.panel;
  return (
    <li
      id={`settings-nav-${item.id}`}
      className={cn("settings-nav-item", active && "active")}
      role="tab"
      tabIndex={active ? 0 : -1}
      aria-selected={active}
      aria-controls={`panel-${item.panel}`}
      onClick={() => item.panel && props.onSelectPanel(item.panel)}
      onKeyDown={(e) =>
        item.panel && props.onPanelKeyDown(e, item as NavItem & { panel: string })
      }
    >
      <NavIcon icon={item.icon} />
      {item.name}
    </li>
  );
}
