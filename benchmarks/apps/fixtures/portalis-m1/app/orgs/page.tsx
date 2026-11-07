import Link from "next/link";
import { requireUser } from "@/lib/auth";
import { listOrgs } from "@/lib/data";

export default async function Orgs() {
  const user = await requireUser();
  const rows = await listOrgs(user.id);
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Your organizations</h1>
        {rows.length > 0 && <Link href="/orgs/new" data-testid="create-org-link" className="rounded bg-slate-900 px-3 py-1.5 text-white">New organization</Link>}
      </div>
      {rows.length === 0 ? (
        <div data-testid="orgs-empty-state" className="space-y-2 rounded border bg-white p-6">
          <p className="text-slate-500">You do not belong to any organization yet.</p>
          <Link href="/orgs/new" data-testid="create-org-link" className="underline">Create one</Link>
        </div>
      ) : (
        <ul data-testid="org-list" className="grid gap-3">
          {rows.map((o) => (
            <li key={o.id}>
              <Link href={`/orgs/${o.id}`} data-testid="org-card" data-org-id={o.id} className="block rounded border bg-white p-4">
                <span data-testid="org-card-name" className="font-medium">{o.name}</span>
                <span className="block text-sm text-slate-500">{o.description}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
