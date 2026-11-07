import Link from "next/link";
import { notFound } from "next/navigation";
import { OrderCart } from "@/components/order-cart";
import { requireUser } from "@/lib/auth";
import { getRestaurant, menuOf } from "@/lib/data";

export default async function RestaurantPage({ params }: { params: Promise<{ id: string }> }) {
  const user = await requireUser();
  const r = await getRestaurant((await params).id);
  if (!r) notFound();
  const menu = await menuOf(r.id);
  return (
    <div className="space-y-3">
      <h1 className="text-xl font-semibold">{r.name}</h1>
      <p className="text-sm text-slate-500">{r.cuisine} · {r.address}</p>
      {r.ownerId === user.id && <Link href={`/restaurants/${r.id}/manage`} className="text-sm underline">Manage menu</Link>}
      <OrderCart restaurantId={r.id} menu={menu.map((m) => ({ id: m.id, name: m.name, description: m.description, priceCents: m.priceCents }))} />
    </div>
  );
}
