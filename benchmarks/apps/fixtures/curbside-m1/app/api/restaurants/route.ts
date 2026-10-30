import { desc } from "drizzle-orm";
import { NextResponse } from "next/server";
import { restaurants } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { parseRestaurant } from "@/lib/data";
import { db } from "@/lib/db";

export async function GET() {
  if (!(await currentUser())) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  return NextResponse.json(await db.select().from(restaurants).orderBy(desc(restaurants.createdAt)));
}

export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const r = parseRestaurant(await req.json().catch(() => null));
  if ("error" in r) return NextResponse.json(r, { status: 400 });
  const [row] = await db.insert(restaurants).values({ ...r, ownerId: user.id }).returning();
  return NextResponse.json(row, { status: 201 });
}
