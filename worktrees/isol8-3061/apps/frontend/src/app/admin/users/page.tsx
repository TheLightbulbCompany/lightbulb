import Link from "next/link";
import { auth } from "@clerk/nextjs/server";

import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { StatusChip } from "@/components/admin/StatusChip";
import {
  listUsers,
  type UserDirectoryRow,
  type UsersPage as UsersPageData,
} from "@/app/admin/_lib/api";
import { formatRelativeTime } from "@/app/admin/_lib/time";

import { UsersClientHeader } from "./UsersClientHeader";
import { truncate } from "@/lib/utils";

export const metadata = { title: "Users \u00b7 Admin" };

function RelativeTimestamp({ value }: { value: number | string | null | undefined }) {
  const when = formatRelativeTime(value);
  return <span title={when.iso ?? undefined}>{when.label}</span>;
}

interface UsersPageProps {
  searchParams: Promise<{ q?: string; cursor?: string }>;
}

export default async function UsersPage({ searchParams }: UsersPageProps) {
  const params = await searchParams;
  const { getToken } = await auth();
  const token = await getToken();
  const result: UsersPageData = token
    ? await listUsers(token, params.q, params.cursor)
    : { users: [], cursor: null, stubbed: false };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-foreground">Users</h1>
      </div>

      <UsersClientHeader defaultValue={params.q} />

      {result.stubbed ? (
        <ErrorBanner
          error="Clerk Backend API key not configured \u2014 list is stubbed."
          variant="warning"
        />
      ) : null}

      {result.users.length === 0 ? (
        params.q ? (
          <EmptyState
            title="No users match your search"
            body="Try a different email or Clerk ID."
          />
        ) : (
          <EmptyState
            title="No users yet"
            body="Once a user signs up, they'll appear here."
          />
        )
      ) : (
        <UsersTable users={result.users} />
      )}

      {result.cursor ? (
        <div className="flex justify-center">
          <Link
            href={{
              pathname: "/admin/users",
              query: params.q
                ? { cursor: result.cursor, q: params.q }
                : { cursor: result.cursor },
            }}
            className="rounded-md border border-border bg-card px-4 py-2 text-sm text-foreground hover:bg-accent"
          >
            Load more
          </Link>
        </div>
      ) : null}
    </div>
  );
}

function UsersTable({ users }: { users: UserDirectoryRow[] }) {
  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="w-full min-w-[720px] text-sm">
        <thead className="bg-card text-left text-xs uppercase tracking-caps text-muted-foreground">
          <tr>
            <th className="px-3 py-2 font-medium">Email</th>
            <th className="px-3 py-2 font-medium">Clerk ID</th>
            <th className="px-3 py-2 font-medium">Plan</th>
            <th className="px-3 py-2 font-medium">Container</th>
            <th className="px-3 py-2 font-medium">Signup</th>
            <th className="px-3 py-2 font-medium">Last sign-in</th>
            <th className="px-3 py-2 font-medium">Banned</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr
              key={u.clerk_id}
              className="border-t border-border transition-colors hover:bg-accent"
            >
              <td className="px-3 py-2">
                <Link
                  href={`/admin/users/${encodeURIComponent(u.clerk_id)}`}
                  className="text-info hover:underline"
                >
                  {u.email ?? <span className="text-muted-foreground">(no email)</span>}
                </Link>
                {u.org ? (
                  <span
                    className="ml-2 font-mono text-2xs text-muted-foreground"
                    title={`Org: ${u.org.name} (${u.org.role})`}
                  >
                    [org:{u.org.slug}]
                  </span>
                ) : null}
              </td>
              <td className="px-3 py-2 font-mono text-xs text-muted-foreground" title={u.clerk_id}>
                {truncate(u.clerk_id, 12)}
              </td>
              <td className="px-3 py-2 text-muted-foreground">{u.subscription_status ?? "—"}</td>
              <td className="px-3 py-2">
                <StatusChip status={u.container_status} />
              </td>
              <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                <RelativeTimestamp value={u.created_at} />
              </td>
              <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                <RelativeTimestamp value={u.last_sign_in_at} />
              </td>
              <td className="px-3 py-2">
                {u.banned ? (
                  <StatusChip status="error" label="Yes" />
                ) : (
                  <span className="text-xs text-muted-foreground">No</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
