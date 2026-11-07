import Link from "next/link";
import { asc } from "drizzle-orm";
import { EditRow } from "@/components/edit-row";
import { practitioners } from "@/db/schema";
import { requireUser } from "@/lib/auth";
import { db } from "@/lib/db";

export default async function Practitioners() {
  await requireUser();
  const rows = await db.select().from(practitioners).orderBy(asc(practitioners.createdAt));
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Practitioners</h1>
        <Link href="/practitioners/new" data-testid="practitioner-new-button" className="rounded bg-slate-900 px-3 py-1.5 text-white">Add practitioner</Link>
      </div>
      <ul data-testid="practitioners-list" className="divide-y rounded border bg-white">
        {rows.map((p) => <EditRow key={p.id} kind="practitioners" id={p.id} fields={[{ key: "name", label: "Name", value: p.name }, { key: "specialty", label: "Specialty", value: p.specialty }]} />)}
      </ul>
    </div>
  );
}
