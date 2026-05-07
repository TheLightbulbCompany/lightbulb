"use client";

import { Users, ExternalLink } from "lucide-react";
import {
  PanelMasthead,
  PanelEmptyState,
  PanelDataTable,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Member {
  id: string;
  user_id?: string;
  role?: string;
  email_via_clerk?: string;
  joined_at?: string;
}

const CLERK_DASHBOARD_URL = "https://dashboard.clerk.com";

export function MembersPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<{ members: Member[] }>("/members");

  if (error) {
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
        Error: {String(error)}
      </div>
    );
  }

  const members = data?.members ?? [];

  const columns: PanelDataColumn<Member>[] = [
    {
      key: "email",
      header: "Member",
      cell: (m) => (
        <span className="text-[var(--ink)]">
          {m.email_via_clerk ?? m.user_id ?? m.id}
        </span>
      ),
      sortable: true,
    },
    {
      key: "role",
      header: "Role",
      cell: (m) =>
        m.role ? (
          <span className="text-xs uppercase tracking-wide text-[var(--mute)]">
            {m.role}
          </span>
        ) : (
          "—"
        ),
    },
    {
      key: "joined",
      header: "Joined",
      align: "right",
      cell: (m) =>
        m.joined_at ? (
          <span className="font-mono text-xs text-[var(--mute)]">{m.joined_at}</span>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Team"
        title="Members"
        actions={
          // Render as a plain anchor styled with the editorial-btn classes so
          // we don't nest interactive controls (`<a><button>` is invalid HTML
          // and breaks keyboard / screen-reader activation). Same visual as
          // <EditorialButton variant="secondary">.
          <a
            href={CLERK_DASHBOARD_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Manage in Clerk"
            className="editorial-btn editorial-btn--secondary editorial-btn--md"
          >
            <Users className="size-4" />
            Manage in Clerk
            <ExternalLink className="size-3" />
          </a>
        }
      />
      <p className="text-sm text-[var(--ink-soft)] -mt-3 italic font-editorial-serif">
        Member management is handled by Clerk.
      </p>

      {isLoading && members.length === 0 ? (
        <div
          role="status"
          aria-live="polite"
          className="font-editorial-serif text-xl italic text-[var(--mute)]"
        >
          Loading…
        </div>
      ) : members.length === 0 ? (
        <PanelEmptyState
          icon={Users}
          headline="No members yet."
          supporting="Invite teammates from the Clerk dashboard. They'll appear here once they accept."
        />
      ) : (
        <PanelDataTable<Member>
          rows={members}
          columns={columns}
          rowKey={(m) => m.id}
        />
      )}
    </div>
  );
}
