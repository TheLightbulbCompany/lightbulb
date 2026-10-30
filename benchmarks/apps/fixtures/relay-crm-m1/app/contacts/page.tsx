import Link from "next/link";
import { ContactsTable } from "@/components/contacts-table";
import { requireUser } from "@/lib/auth";
import { listContacts } from "@/lib/data";

export default async function Contacts() {
  const user = await requireUser();
  const rows = await listContacts(user.id);
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Contacts</h1>
        <div className="flex gap-2">
          <Link href="/contacts/import" className="rounded border px-3 py-1.5">Import CSV</Link>
          <Link href="/contacts/new" data-testid="contact-new-button" className="rounded bg-slate-900 px-3 py-1.5 text-white">New contact</Link>
        </div>
      </div>
      <ContactsTable rows={rows} />
    </div>
  );
}
