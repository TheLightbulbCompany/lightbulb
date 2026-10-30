import Link from "next/link";
import { inArray } from "drizzle-orm";
import { restaurants } from "@/db/schema";
import { requireUser } from "@/lib/auth";
import { db } from "@/lib/db";
import { listOrders } from "@/lib/data";
import { dollars } from "@/lib/money";

export default async function Orders() {
  const user = await requireUser();
  const rows = await listOrders(user.id);
  const names = new Map((rows.length ? await db.select({ id: restaurants.id, name: restaurants.name }).from(restaurants).where(inArray(restaurants.id, rows.map((o) => o.restaurantId))) : []).map((r) => [r.id, r.name]));
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Your orders</h1>
      {rows.length === 0 ? (
        <p data-testid="orders-empty" className="text-slate-500">No orders yet.</p>
      ) : (
        <ul data-testid="orders-list" className="divide-y rounded border bg-white">
          {rows.map((o) => (
            <li key={o.id} data-testid="order-row" data-order-id={o.id} className="flex items-center justify-between px-4 py-3">
              <Link href={`/orders/${o.id}`} className="underline">{names.get(o.restaurantId)}</Link>
              <span className="flex gap-3 text-sm text-slate-500">
                <span data-testid="order-row-status">{o.status}</span>
                <span data-cents={o.totalCents}>{dollars(o.totalCents)}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
