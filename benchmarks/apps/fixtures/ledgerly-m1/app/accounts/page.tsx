import Link from "next/link";
import { requireUser } from "@/lib/auth";
import { listAccounts } from "@/lib/data";

export default async function Accounts() {
  const user = await requireUser();
  const rows = await listAccounts(user.id);
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Chart of accounts</h1>
        <Link href="/accounts/new" data-testid="account-new-button" className="rounded bg-slate-900 px-3 py-1.5 text-white">New account</Link>
      </div>
      {rows.length === 0 ? (
        <p data-testid="accounts-empty" className="text-slate-500">No accounts yet.</p>
      ) : (
        <ul data-testid="accounts-list" className="divide-y rounded border bg-white">
          {rows.map((a) => (
            <li key={a.id} data-testid="account-row" className="flex items-center justify-between px-4 py-3">
              <span><span data-testid="account-row-code" className="font-mono">{a.code}</span> {a.name}</span>
              <span className="text-sm text-slate-500">{a.type}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
