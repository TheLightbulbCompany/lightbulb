import { eq } from "drizzle-orm";
import { NextResponse } from "next/server";
import { tasks } from "@/db/schema";
import { db } from "@/lib/db";
import { getTask } from "@/lib/tasks";

type Ctx = { params: Promise<{ id: string }> };
const no = (status: number, error: string) => NextResponse.json({ error }, { status });

// BROKEN ON PURPOSE: any id, any caller.
export async function GET(_: Request, { params }: Ctx) {
  const task = await getTask(undefined, (await params).id);
  return task ? NextResponse.json(task) : no(404, "Not found");
}

export async function PATCH(req: Request, { params }: Ctx) {
  const b = await req.json().catch(() => ({}));
  const [row] = await db.update(tasks).set({ done: !!b.done }).where(eq(tasks.id, (await params).id)).returning();
  return row ? NextResponse.json(row) : no(404, "Not found");
}

export async function DELETE(_: Request, { params }: Ctx) {
  const [row] = await db.delete(tasks).where(eq(tasks.id, (await params).id)).returning({ id: tasks.id });
  return row ? NextResponse.json({ ok: true }) : no(404, "Not found");
}
