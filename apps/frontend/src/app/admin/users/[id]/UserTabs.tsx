"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

export interface UserTab {
  label: string;
  /** Path segment after `/admin/users/{id}`. Empty string = Overview. */
  segment: string;
}

export interface UserTabsProps {
  userId: string;
  tabs: UserTab[];
}

/**
 * Tab strip for the per-user admin pages. Active tab is detected via
 * `usePathname()` so this stays a tiny client component while the surrounding
 * layout + page bodies remain server-rendered.
 */
export function UserTabs({ userId, tabs }: UserTabsProps) {
  const pathname = usePathname() ?? "";
  const base = `/admin/users/${encodeURIComponent(userId)}`;
  // Overview matches the bare base path; sub-tabs match `${base}/${segment}`.
  const matchSegment = pathname === base || pathname === `${base}/`
    ? ""
    : pathname.startsWith(`${base}/`)
      ? pathname.slice(base.length + 1).split("/")[0] ?? ""
      : "";

  return (
    <nav
      role="tablist"
      aria-label="User detail sections"
      className="flex items-center gap-1 border-b border-border"
    >
      {tabs.map((tab) => {
        const href = tab.segment ? `${base}/${tab.segment}` : base;
        const active = tab.segment === matchSegment;
        return (
          <Link
            key={tab.segment || "overview"}
            href={href}
            role="tab"
            aria-selected={active}
            className={cn(
              "px-4 py-2 text-sm transition-colors",
              "border-b-2",
              active
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:border-border hover:text-foreground",
            )}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
