import Link from "next/link";
import { notFound } from "next/navigation";
import { ContactForm } from "@/components/contact-form";
import { DeleteButton } from "@/components/delete-button";
import { requireUser } from "@/lib/auth";
import { getContact, listCompanies } from "@/lib/data";

export default async function ContactPage({ params }: { params: Promise<{ id: string }> }) {
  const user = await requireUser();
  const c = await getContact(user.id, (await params).id);
  if (!c) notFound();
  const cs = await listCompanies(user.id);
  return (
    <div className="space-y-4">
      <h1 data-testid="contact-detail-name" className="text-xl font-semibold">{c.name}</h1>
      <p className="text-slate-600">{c.title} {c.title && c.companyName ? "at" : ""} {c.companyId ? <Link href={`/companies/${c.companyId}`} data-testid="contact-detail-company" className="underline">{c.companyName}</Link> : <span data-testid="contact-detail-company" />}</p>
      <p className="text-sm text-slate-500">{c.email} {c.phone}</p>
      <ContactForm initial={c} companies={cs.map((x) => ({ id: x.id, name: x.name }))} />
      <DeleteButton path={`contacts/${c.id}`} after="/contacts" label="contact" />
    </div>
  );
}
