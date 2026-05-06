"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  Users,
  ListTodo,
  ClipboardCheck,
  Repeat,
  Target,
  FolderKanban,
  History,
  DollarSign,
  Boxes,
  UsersRound,
  Settings,
  Inbox,
  Network,
  ArrowLeft,
} from "lucide-react";

const ITEMS: {
  key: string;
  label: string;
  Icon: React.ComponentType<{ className?: string }>;
}[] = [
  { key: "dashboard", label: "Dashboard", Icon: LayoutDashboard },
  { key: "agents", label: "Agents", Icon: Users },
  { key: "org-chart", label: "Org chart", Icon: Network },
  { key: "inbox", label: "Inbox", Icon: Inbox },
  { key: "approvals", label: "Approvals", Icon: ClipboardCheck },
  { key: "issues", label: "Issues", Icon: ListTodo },
  { key: "routines", label: "Routines", Icon: Repeat },
  { key: "goals", label: "Goals", Icon: Target },
  { key: "projects", label: "Projects", Icon: FolderKanban },
  { key: "activity", label: "Activity", Icon: History },
  { key: "costs", label: "Costs", Icon: DollarSign },
  { key: "skills", label: "Skills", Icon: Boxes },
  { key: "members", label: "Members", Icon: UsersRound },
  { key: "settings", label: "Settings", Icon: Settings },
];

export function TeamsSidebar() {
  const pathname = usePathname();
  return (
    <aside className="flex w-60 flex-col border-r border-[var(--edge)] bg-[var(--cream-deeper)]">
      <div className="border-b border-[var(--edge)] px-5 py-5">
        <Link
          href="/teams/dashboard"
          className="font-editorial-serif text-[18px] font-bold uppercase tracking-[0.14em] text-[var(--ink)] no-underline"
        >
          ISOL8
        </Link>
        <div className="mt-0.5 text-[11px] uppercase tracking-[0.14em] text-[var(--mute)]">
          Teams
        </div>
      </div>
      <nav className="flex-1 overflow-y-auto px-2 py-3">
        {ITEMS.map(({ key, label, Icon }) => {
          const active = pathname?.startsWith(`/teams/${key}`);
          return (
            <Link
              key={key}
              href={`/teams/${key}`}
              className={`mb-0.5 flex items-center gap-2.5 rounded-full px-3 py-2 text-sm no-underline transition-colors ${
                active
                  ? "bg-[var(--cream)] text-[var(--forest)]"
                  : "text-[var(--ink-soft)] hover:bg-[var(--forest-tint)] hover:text-[var(--forest)]"
              }`}
            >
              <Icon className="h-4 w-4" />
              {label}
            </Link>
          );
        })}
      </nav>
      <div className="border-t border-[var(--edge)] p-3">
        <Link
          href="/chat"
          className="flex items-center gap-2 rounded-full px-3 py-2 text-sm text-[var(--mute)] no-underline hover:bg-[var(--forest-tint)] hover:text-[var(--forest)]"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to chat
        </Link>
      </div>
    </aside>
  );
}
