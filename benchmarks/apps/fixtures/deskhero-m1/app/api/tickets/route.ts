import { NextResponse } from "next/server";
import { tickets } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { db } from "@/lib/db";
import { listTickets, parseTicket } from "@/lib/tickets";

export async function GET() {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  return NextResponse.json(await listTickets(user.id));
}

export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const t = parseTicket(await req.json().catch(() => null));
  if ("error" in t) return NextResponse.json(t, { status: 400 });
  const [row] = await db.insert(tickets).values({ ...t, userId: user.id }).returning(); // status is always open
  return NextResponse.json(row, { status: 201 });
}
