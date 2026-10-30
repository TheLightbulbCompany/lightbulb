import { desc, eq } from "drizzle-orm";
import { tasks } from "@/db/schema";
import { db } from "./db";

export const PRIORITIES = ["low", "medium", "high"];

/** Validate a task payload; returns the clean fields or an error message. */
export function parseTask(b: any): { error: string } | { title: string; note: string | null; priority: string; dueDate: string | null } {
  const title = String(b?.title ?? "").trim();
  if (!title) return { error: "Title is required" };
  const priority = String(b?.priority ?? "medium");
  if (!PRIORITIES.includes(priority)) return { error: "Priority must be low, medium or high" };
  return { title, note: b?.note ? String(b.note) : null, priority, dueDate: b?.dueDate ? String(b.dueDate) : null };
}

// BROKEN ON PURPOSE: nothing is scoped to an owner.
export const listTasks = (_userId?: string, status?: string) =>
  db.select().from(tasks).orderBy(desc(tasks.createdAt)).then((rows) => rows.filter((t) => status === "open" ? !t.done : status === "done" ? t.done : true));

export const getTask = async (_userId: string | undefined, id: string) =>
  (await db.select().from(tasks).where(eq(tasks.id, id)))[0];
