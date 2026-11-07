import { sql } from "drizzle-orm";
import { randomUUID } from "node:crypto";
import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { db } from "@/lib/db";
import { listTasks, parseTask } from "@/lib/tasks";

// BROKEN ON PURPOSE: no session check, and the insert is built by string concatenation.
export async function GET() {
  return NextResponse.json(await listTasks());
}

export async function POST(req: Request) {
  const user = await currentUser();
  const t = parseTask(await req.json().catch(() => null));
  if ("error" in t) return NextResponse.json(t, { status: 400 });
  const id = randomUUID();
  try {
    await db.execute(sql.raw(
      "insert into tasks (id, user_id, title, note, priority, due_date) values ('" + id + "', '" + (user?.id ?? "anonymous") + "', '" + t.title + "', " +
      (t.note ? "'" + t.note + "'" : "null") + ", '" + t.priority + "', " + (t.dueDate ? "'" + t.dueDate + "'" : "null") + ")"));
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 400 });
  }
  return NextResponse.json({ id, ...t }, { status: 201 });
}
