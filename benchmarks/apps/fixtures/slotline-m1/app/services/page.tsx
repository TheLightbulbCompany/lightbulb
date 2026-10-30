import Link from "next/link";
import { asc } from "drizzle-orm";
import { EditRow } from "@/components/edit-row";
import { services } from "@/db/schema";
import { requireUser } from "@/lib/auth";
import { db } from "@/lib/db";

export default async function Services() {
  await requireUser();
  const rows = await db.select().from(services).orderBy(asc(services.createdAt));
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Services</h1>
        <Link href="/services/new" data-testid="service-new-button" className="rounded bg-slate-900 px-3 py-1.5 text-white">Add service</Link>
      </div>
      <ul data-testid="services-list" className="divide-y rounded border bg-white">
        {rows.map((s) => <EditRow key={s.id} kind="services" id={s.id} fields={[{ key: "name", label: "Name", value: s.name }, { key: "durationMinutes", label: "Minutes", value: s.durationMinutes, type: "number" }]} />)}
      </ul>
    </div>
  );
}
