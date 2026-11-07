import { notFound } from "next/navigation";
import { MenuManager } from "@/components/menu-manager";
import { requireUser } from "@/lib/auth";
import { getRestaurant, menuOf } from "@/lib/data";

export default async function Manage({ params }: { params: Promise<{ id: string }> }) {
  const user = await requireUser();
  const r = await getRestaurant((await params).id);
  if (!r || r.ownerId !== user.id) notFound(); // only the restaurant's owner
  const menu = await menuOf(r.id);
  return (
    <div className="space-y-3">
      <h1 className="text-xl font-semibold">{r.name} menu</h1>
      <MenuManager restaurantId={r.id} menu={menu.map((m) => ({ id: m.id, name: m.name, description: m.description, priceCents: m.priceCents }))} />
    </div>
  );
}
