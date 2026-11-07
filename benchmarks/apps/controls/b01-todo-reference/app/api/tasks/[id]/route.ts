import { and, eq } from "drizzle-orm";
import { NextResponse } from "next/server";
import { tasks } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { db } from "@/lib/db";
import { getTask, parseTask } from "@/lib/tasks";

type Ctx = { params: Promise<{ id: string }> };
const mine = (userId: string, id: string) => and(eq(tasks.id, id), eq(tasks.userId, userId));
const no = (status: number, error: string) => NextResponse.json({ error }, { status });

export async function GET(_: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const task = await getTask(user.id, (await params).id);
  return task ? NextResponse.json(task) : no(404, "Not found");
}

// Body: { done: boolean } to complete/reopen, or the task fields to edit.
export async function PATCH(req: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const b = await req.json().catch(() => ({}));
  let set: Partial<typeof tasks.$inferInsert>;
  if (typeof b.done === "boolean") set = { done: b.done };
  else {
    const t = parseTask(b);
    if ("error" in t) return no(400, t.error);
    set = t;
  }
  const [row] = await db.update(tasks).set(set).where(mine(user.id, (await params).id)).returning();
  return row ? NextResponse.json(row) : no(404, "Not found");
}

export async function DELETE(_: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const [row] = await db.delete(tasks).where(mine(user.id, (await params).id)).returning({ id: tasks.id });
  return row ? NextResponse.json({ ok: true }) : no(404, "Not found");
}
