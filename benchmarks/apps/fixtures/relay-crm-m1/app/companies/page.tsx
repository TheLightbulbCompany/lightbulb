import Link from "next/link";
import { CompanyForm } from "@/components/company-form";
import { requireUser } from "@/lib/auth";
import { listCompanies } from "@/lib/data";

export default async function Companies() {
  const user = await requireUser();
  const rows = await listCompanies(user.id);
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Companies</h1>
      {rows.length === 0 ? (
        <p data-testid="companies-empty" className="text-slate-500">No companies yet.</p>
      ) : (
        <ul data-testid="companies-list" className="divide-y rounded border bg-white">
          {rows.map((c) => (
            <li key={c.id}>
              <Link href={`/companies/${c.id}`} data-testid="company-row" className="flex items-center justify-between px-4 py-3">
                <span data-testid="company-row-name" className="font-medium">{c.name}</span>
                <span className="text-sm text-slate-500">{c.domain}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
      <CompanyForm />
    </div>
  );
}
