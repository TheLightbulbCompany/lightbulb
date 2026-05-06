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
    <aside className="w-60 border-r bg-zinc-50 flex flex-col">
      <div className="p-4 border-b">
        <Link href="/chat" className="text-sm text-zinc-600 hover:underline">
          ← Back to chat
        </Link>
      </div>
      <nav className="flex-1 overflow-y-auto p-2">
        {ITEMS.map(({ key, label, Icon }) => {
          const active = pathname?.startsWith(`/teams/${key}`);
          return (
            <Link
              key={key}
              href={`/teams/${key}`}
              className={`flex items-center gap-2 px-3 py-2 rounded text-sm ${
                active
                  ? "bg-zinc-200 text-zinc-900"
                  : "text-zinc-700 hover:bg-zinc-100"
              }`}
            >
              <Icon className="w-4 h-4" />
              {label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
