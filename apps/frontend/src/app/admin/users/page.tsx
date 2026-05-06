import Link from "next/link";
import { auth } from "@clerk/nextjs/server";

import { EmptyState } from "@/components/admin/EmptyState";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import {
  listUsers,
  type UserDirectoryRow,
  type UsersPage as UsersPageData,
} from "@/app/admin/_lib/api";

import { UsersClientHeader } from "./UsersClientHeader";

export const metadata = { title: "Users \u00b7 Admin" };

const CONTAINER_STATUS_CLASSES: Record<string, string> = {
  running: "bg-emerald-500/15 text-emerald-300",
  provisioning: "bg-sky-500/15 text-sky-300",
  stopped: "bg-zinc-500/15 text-zinc-300",
  error: "bg-red-500/15 text-red-300",
  unknown: "bg-zinc-500/15 text-zinc-400",
};

function statusClass(status: string): string {
  return CONTAINER_STATUS_CLASSES[status] ?? CONTAINER_STATUS_CLASSES.unknown;
}

function truncateId(id: string, head = 12): string {
  if (id.length <= head) return id;
  return `${id.slice(0, head)}\u2026`;
}

function formatTimestamp(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "\u2014";
  // Clerk timestamps come as numeric ms-since-epoch. DDB rows may store
  // ISO strings. Render either gracefully.
  if (typeof value === "number") {
    try {
      return new Date(value).toISOString().slice(0, 10);
    } catch {
      return String(value);
    }
  }
  return value;
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
        <h1 className="text-2xl font-semibold text-zinc-100">Users</h1>
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
            className="rounded-md border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm text-zinc-200 hover:bg-zinc-800"
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
    <div className="overflow-x-auto rounded-md border border-white/10">
      <table className="w-full min-w-[720px] text-sm">
        <thead className="bg-zinc-900 text-left text-xs uppercase tracking-wide text-zinc-400">
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
              className="border-t border-white/5 transition-colors hover:bg-white/[0.03]"
            >
              <td className="px-3 py-2">
                <Link
                  href={`/admin/users/${encodeURIComponent(u.clerk_id)}`}
                  className="text-sky-300 hover:underline"
                >
                  {u.email ?? <span className="text-zinc-500">(no email)</span>}
                </Link>
                {u.org ? (
                  <span
                    className="ml-2 font-mono text-[10px] text-indigo-300/80"
                    title={`Org: ${u.org.name} (${u.org.role})`}
                  >
                    [org:{u.org.slug}]
                  </span>
                ) : null}
              </td>
              <td className="px-3 py-2 font-mono text-xs text-zinc-300" title={u.clerk_id}>
                {truncateId(u.clerk_id)}
              </td>
              <td className="px-3 py-2 text-zinc-300">{u.subscription_status ?? "—"}</td>
              <td className="px-3 py-2">
                <span
                  className={`rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${statusClass(u.container_status)}`}
                >
                  {u.container_status}
                </span>
              </td>
              <td className="px-3 py-2 font-mono text-xs text-zinc-300">
                {formatTimestamp(u.created_at)}
              </td>
              <td className="px-3 py-2 font-mono text-xs text-zinc-300">
                {formatTimestamp(u.last_sign_in_at)}
              </td>
              <td className="px-3 py-2">
                {u.banned ? (
                  <span className="rounded bg-red-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-red-300">
                    Yes
                  </span>
                ) : (
                  <span className="text-xs text-zinc-500">No</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
