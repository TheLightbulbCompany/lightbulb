import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { getRestaurant, menuOf } from "@/lib/data";

export async function GET(_: Request, { params }: { params: Promise<{ id: string }> }) {
  if (!(await currentUser())) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const r = await getRestaurant((await params).id);
  if (!r) return NextResponse.json({ error: "Not found" }, { status: 404 });
  return NextResponse.json({ ...r, menuItems: await menuOf(r.id) });
}
