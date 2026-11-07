import Link from "next/link";
import { desc } from "drizzle-orm";
import { restaurants } from "@/db/schema";
import { requireUser } from "@/lib/auth";
import { db } from "@/lib/db";

export default async function Restaurants() {
  await requireUser();
  const rows = await db.select().from(restaurants).orderBy(desc(restaurants.createdAt)); // the directory is shared
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Restaurants</h1>
        <Link href="/restaurants/new" data-testid="restaurant-new-button" className="rounded bg-slate-900 px-3 py-1.5 text-white">Add restaurant</Link>
      </div>
      {rows.length === 0 ? (
        <p className="text-slate-500">No restaurants yet.</p>
      ) : (
        <ul data-testid="restaurants-list" className="divide-y rounded border bg-white">
          {rows.map((r) => (
            <li key={r.id} data-testid="restaurant-row" data-restaurant-id={r.id} className="flex items-center justify-between px-4 py-3">
              <Link href={`/restaurants/${r.id}`} data-testid="restaurant-row-link" className="underline">{r.name}</Link>
              <span className="text-sm text-slate-500">{r.cuisine} · {r.address}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
