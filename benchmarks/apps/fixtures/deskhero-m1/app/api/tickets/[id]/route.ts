import { and, eq } from "drizzle-orm";
import { NextResponse } from "next/server";
import { tickets } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { db } from "@/lib/db";
import { getTicket, parseTicket } from "@/lib/tickets";

type Ctx = { params: Promise<{ id: string }> };
const mine = (userId: string, id: string) => and(eq(tickets.id, id), eq(tickets.userId, userId));
const no = (status: number, error: string) => NextResponse.json({ error }, { status });

export async function GET(_: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const ticket = await getTicket(user.id, (await params).id);
  return ticket ? NextResponse.json(ticket) : no(404, "Not found");
}

// Body: { status: "open" | "closed" } to close/reopen, or the ticket fields to edit.
export async function PATCH(req: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const b = await req.json().catch(() => ({}));
  let set: Partial<typeof tickets.$inferInsert>;
  if (b.status !== undefined) {
    if (b.status !== "open" && b.status !== "closed") return no(400, "Status must be open or closed");
    set = { status: b.status };
  } else {
    const t = parseTicket(b);
    if ("error" in t) return no(400, t.error);
    set = t;
  }
  const [row] = await db.update(tickets).set(set).where(mine(user.id, (await params).id)).returning();
  return row ? NextResponse.json(row) : no(404, "Not found");
}

export async function DELETE(_: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const [row] = await db.delete(tickets).where(mine(user.id, (await params).id)).returning({ id: tickets.id });
  return row ? NextResponse.json({ ok: true }) : no(404, "Not found");
}
