import Link from "next/link";

import { UserTabs, type UserTab } from "./UserTabs";

const TABS: UserTab[] = [
  { label: "Overview", segment: "" },
  { label: "Agents", segment: "agents" },
  { label: "Billing", segment: "billing" },
  { label: "Container", segment: "container" },
  { label: "Activity", segment: "activity" },
  { label: "Actions", segment: "actions" },
  { label: "Logs", segment: "logs" },
];

interface UserLayoutProps {
  children: React.ReactNode;
  params: Promise<{ id: string }>;
}

export default async function UserLayout({ children, params }: UserLayoutProps) {
  const { id } = await params;

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <div className="text-xs text-zinc-500">
          <Link href="/admin/users" className="hover:text-zinc-300">
            Users
          </Link>
          <span className="mx-2 text-zinc-600">/</span>
          <span className="font-mono text-zinc-300">{id}</span>
        </div>
        <h1 className="font-mono text-lg text-zinc-100" title={id}>
          {id}
        </h1>
      </div>

      <UserTabs userId={id} tabs={TABS} />

      <div>{children}</div>
    </div>
  );
}
