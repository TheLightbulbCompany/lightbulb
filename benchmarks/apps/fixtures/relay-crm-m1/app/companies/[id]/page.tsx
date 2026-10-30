import Link from "next/link";
import { notFound } from "next/navigation";
import { CompanyForm } from "@/components/company-form";
import { DeleteButton } from "@/components/delete-button";
import { requireUser } from "@/lib/auth";
import { getCompany } from "@/lib/data";

export default async function CompanyPage({ params }: { params: Promise<{ id: string }> }) {
  const user = await requireUser();
  const c = await getCompany(user.id, (await params).id);
  if (!c) notFound();
  return (
    <div className="space-y-4">
      <h1 data-testid="company-detail-name" className="text-xl font-semibold">{c.name}</h1>
      <p className="text-sm text-slate-500">{c.domain}</p>
      <ul data-testid="company-contacts-list" className="divide-y rounded border bg-white">
        {c.contacts.map((p) => (
          <li key={p.id} data-testid="company-contact-row" className="flex justify-between px-4 py-3">
            <span>{p.name} <span className="text-slate-500">{p.email}</span></span>
            <Link href={`/contacts/${p.id}`} className="underline">Open</Link>
          </li>
        ))}
      </ul>
      <CompanyForm initial={{ id: c.id, name: c.name, domain: c.domain }} />
      <DeleteButton path={`companies/${c.id}`} after="/companies" label="company" />
    </div>
  );
}
