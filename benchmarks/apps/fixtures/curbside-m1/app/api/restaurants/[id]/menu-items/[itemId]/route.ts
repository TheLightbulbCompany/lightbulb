import { and, eq } from "drizzle-orm";
import { NextResponse } from "next/server";
import { menuItems } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { getRestaurant } from "@/lib/data";
import { db } from "@/lib/db";

export async function DELETE(_: Request, { params }: { params: Promise<{ id: string; itemId: string }> }) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const { id, itemId } = await params;
  const r = await getRestaurant(id);
  if (!r || r.ownerId !== user.id) return NextResponse.json({ error: "Not found" }, { status: 404 });
  try {
    const [row] = await db.delete(menuItems).where(and(eq(menuItems.id, itemId), eq(menuItems.restaurantId, r.id))).returning({ id: menuItems.id });
    return row ? NextResponse.json({ ok: true }) : NextResponse.json({ error: "Not found" }, { status: 404 });
  } catch {
    return NextResponse.json({ error: "That item is on an existing order" }, { status: 409 }); // order lines keep a reference to it
  }
}
