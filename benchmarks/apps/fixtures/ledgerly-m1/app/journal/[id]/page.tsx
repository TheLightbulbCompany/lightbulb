import { notFound } from "next/navigation";
import { requireUser } from "@/lib/auth";
import { getEntry, listAccounts } from "@/lib/data";
import { fmtCents } from "@/lib/money";

export default async function EntryPage({ params }: { params: Promise<{ id: string }> }) {
  const user = await requireUser();
  const e = await getEntry(user.id, (await params).id);
  if (!e) notFound();
  const label = new Map((await listAccounts(user.id)).map((a) => [a.id, `${a.code} ${a.name}`]));
  return (
    <div className="space-y-3">
      <h1 className="text-xl font-semibold">{e.memo || "Journal entry"}</h1>
      <p>Date: <span data-testid="entry-detail-date" data-entry-date={e.date}>{e.date}</span></p>
      <table className="w-full rounded border bg-white text-sm">
        <thead><tr className="text-left"><th className="p-2">Account</th><th className="p-2">Debit</th><th className="p-2">Credit</th></tr></thead>
        <tbody>
          {e.lines.map((l) => (
            <tr key={l.id} className="border-t">
              <td className="p-2">{label.get(l.accountId)}</td>
              <td className="p-2">{l.debitCents ? fmtCents(l.debitCents) : ""}</td>
              <td className="p-2">{l.creditCents ? fmtCents(l.creditCents) : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>Total debits: <span data-testid="entry-detail-total-debit">{fmtCents(e.totalDebitCents)}</span></p>
      <p>Total credits: <span data-testid="entry-detail-total-credit">{fmtCents(e.totalCreditCents)}</span></p>
    </div>
  );
}
