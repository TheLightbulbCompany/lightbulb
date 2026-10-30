import { NextResponse } from "next/server";
import { menuItems } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { getRestaurant, parseMenuItem } from "@/lib/data";
import { db } from "@/lib/db";

export async function POST(req: Request, { params }: { params: Promise<{ id: string }> }) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const r = await getRestaurant((await params).id);
  if (!r || r.ownerId !== user.id) return NextResponse.json({ error: "Not found" }, { status: 404 }); // only the owner manages the menu
  const item = parseMenuItem(await req.json().catch(() => null));
  if ("error" in item) return NextResponse.json(item, { status: 400 });
  const [row] = await db.insert(menuItems).values({ ...item, restaurantId: r.id }).returning();
  return NextResponse.json(row, { status: 201 });
}
