import { and, desc, eq } from "drizzle-orm";
import { tickets } from "@/db/schema";
import { db } from "./db";

export const PRIORITIES = ["low", "medium", "high"];

/** Validate the editable fields of a ticket; returns the clean fields or an error message. */
export function parseTicket(b: any): { error: string } | { subject: string; body: string; priority: string } {
  const subject = String(b?.subject ?? "").trim();
  if (!subject) return { error: "Subject is required" };
  const priority = String(b?.priority ?? "medium");
  if (!PRIORITIES.includes(priority)) return { error: "Priority must be low, medium or high" };
  return { subject, body: String(b?.body ?? ""), priority };
}

/** Every query is scoped to the creator. */
export const listTickets = (userId: string) =>
  db.select().from(tickets).where(eq(tickets.userId, userId)).orderBy(desc(tickets.createdAt));

export const getTicket = async (userId: string, id: string) =>
  (await db.select().from(tickets).where(and(eq(tickets.id, id), eq(tickets.userId, userId))))[0];
