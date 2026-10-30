"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { dollarsToCents } from "@/lib/money";

const input = "w-full rounded border px-3 py-2";

export function EntryForm({ accounts }: { accounts: { id: string; label: string }[] }) {
  const router = useRouter();
  const [error, setError] = useState("");
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const lines: { accountId: string; debitCents: number; creditCents: number }[] = [];
    for (let i = 0; i < 3; i++) {
      const accountId = String(f.get(`account${i}`) ?? ""), debit = String(f.get(`debit${i}`) ?? "").trim(), credit = String(f.get(`credit${i}`) ?? "").trim();
      if (!accountId && !debit && !credit) continue; // a blank row is ignored
      const debitCents = debit ? dollarsToCents(debit) : 0, creditCents = credit ? dollarsToCents(credit) : 0;
      if (debitCents === null || creditCents === null) return setError("Enter amounts in dollars with at most two decimals");
      lines.push({ accountId, debitCents, creditCents });
    }
    const r = await fetch("/api/entries", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ date: f.get("date"), memo: f.get("memo"), lines }) });
    if (!r.ok) return setError((await r.json().catch(() => null))?.error ?? "Something went wrong");
    router.push("/journal");
    router.refresh();
  }
  return (
    <form onSubmit={submit} noValidate className="space-y-3">
      <h1 className="text-xl font-semibold">New journal entry</h1>
      <input name="date" type="date" data-testid="entry-form-date" className={input} />
      <input name="memo" placeholder="Memo" data-testid="entry-form-memo" className={input} />
      {[0, 1, 2].map((i) => (
        <div key={i} data-testid="entry-line-row" className="grid grid-cols-3 gap-2">
          <select name={`account${i}`} defaultValue="" data-testid="line-account" className={input}>
            <option value="">Account</option>
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select>
          <input name={`debit${i}`} inputMode="decimal" placeholder="Debit" data-testid="line-debit" className={input} />
          <input name={`credit${i}`} inputMode="decimal" placeholder="Credit" data-testid="line-credit" className={input} />
        </div>
      ))}
      {error && <p role="alert" data-testid="entry-error" className="text-sm text-red-600">{error}</p>}
      <button type="submit" data-testid="entry-submit" className="rounded bg-slate-900 px-4 py-2 text-white">Save entry</button>
    </form>
  );
}
