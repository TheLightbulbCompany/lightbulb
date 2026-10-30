import { and, desc, eq } from "drizzle-orm";
import { tasks } from "@/db/schema";
import { db } from "./db";

export const PRIORITIES = ["low", "medium", "high"];

/** Validate a task payload; returns the clean fields or an error message. */
export function parseTask(b: any): { error: string } | { title: string; note: string | null; priority: string; dueDate: string | null } {
  const title = String(b?.title ?? "").trim();
  if (!title) return { error: "Title is required" };
  const priority = String(b?.priority ?? "medium");
  if (!PRIORITIES.includes(priority)) return { error: "Priority must be low, medium or high" };
  const dueDate = b?.dueDate ? String(b.dueDate) : null;
  if (dueDate && !/^\d{4}-\d{2}-\d{2}$/.test(dueDate)) return { error: "Due date must be YYYY-MM-DD" };
  return { title, note: b?.note ? String(b.note) : null, priority, dueDate };
}

/** Every query is scoped to the owner. */
export const listTasks = (userId: string, status?: string) =>
  db.select().from(tasks)
    .where(and(eq(tasks.userId, userId), status === "open" ? eq(tasks.done, false) : status === "done" ? eq(tasks.done, true) : undefined))
    .orderBy(desc(tasks.createdAt));

export const getTask = async (userId: string, id: string) =>
  (await db.select().from(tasks).where(and(eq(tasks.id, id), eq(tasks.userId, userId))))[0];
