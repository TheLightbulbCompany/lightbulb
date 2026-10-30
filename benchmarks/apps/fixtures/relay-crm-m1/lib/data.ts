import { and, asc, eq } from "drizzle-orm";
import { companies, contacts } from "@/db/schema";
import { db } from "./db";
import { parseCsv } from "./csv";

const text = (v: unknown) => String(v ?? "").trim();
type Err = { error: string; status: number };
const isObj = (b: unknown): b is Record<string, unknown> => !!b && typeof b === "object" && !Array.isArray(b);

// Every query below is scoped to the signed-in person (userId).

const contactCols = { id: contacts.id, name: contacts.name, email: contacts.email, phone: contacts.phone, title: contacts.title, companyId: contacts.companyId, companyName: companies.name };

export const listContacts = (userId: string) =>
  db.select(contactCols).from(contacts).leftJoin(companies, eq(companies.id, contacts.companyId)).where(eq(contacts.userId, userId)).orderBy(asc(contacts.name), asc(contacts.createdAt));

export const getContact = async (userId: string, id: string) =>
  (await db.select(contactCols).from(contacts).leftJoin(companies, eq(companies.id, contacts.companyId)).where(and(eq(contacts.id, id), eq(contacts.userId, userId))))[0];

/** Validated column values from a request body; `partial` (PATCH) only looks at the keys that are present. */
async function contactFields(userId: string, b: unknown, partial: boolean): Promise<Err | { v: Partial<typeof contacts.$inferInsert> }> {
  if (!isObj(b)) return { error: "Send a JSON object", status: 400 };
  const v: Partial<typeof contacts.$inferInsert> = {};
  if (!partial || "name" in b) { v.name = text(b.name); if (!v.name) return { error: "Name is required", status: 400 }; }
  if (!partial || "email" in b) {
    v.email = text(b.email).toLowerCase();
    if (v.email && !/^[^@\s]+@[^@\s]+$/.test(v.email)) return { error: "Enter a valid email", status: 400 };
  }
  if (!partial || "phone" in b) v.phone = text(b.phone);
  if (!partial || "title" in b) v.title = text(b.title);
  if (!partial || "companyId" in b) {
    v.companyId = text(b.companyId) || null;
    if (v.companyId && !(await getCompanyRow(userId, v.companyId))) return { error: "Choose one of your companies", status: 400 };
  }
  return { v };
}

export async function createContact(userId: string, b: unknown): Promise<Err | { contact: NonNullable<Awaited<ReturnType<typeof getContact>>> }> {
  const f = await contactFields(userId, b, false);
  if ("error" in f) return f;
  const [row] = await db.insert(contacts).values({ ...f.v, userId } as typeof contacts.$inferInsert).returning({ id: contacts.id });
  return { contact: (await getContact(userId, row.id))! };
}

export async function updateContact(userId: string, id: string, b: unknown): Promise<Err | { contact: NonNullable<Awaited<ReturnType<typeof getContact>>> }> {
  const f = await contactFields(userId, b, true);
  if ("error" in f) return f;
  if (Object.keys(f.v).length) await db.update(contacts).set(f.v).where(and(eq(contacts.id, id), eq(contacts.userId, userId)));
  const contact = await getContact(userId, id);
  return contact ? { contact } : { error: "Not found", status: 404 };
}

export async function deleteContact(userId: string, id: string) {
  return (await db.delete(contacts).where(and(eq(contacts.id, id), eq(contacts.userId, userId))).returning({ id: contacts.id })).length > 0;
}

export const listCompanies = (userId: string) => db.select().from(companies).where(eq(companies.userId, userId)).orderBy(asc(companies.name), asc(companies.createdAt));
const getCompanyRow = async (userId: string, id: string) => (await db.select().from(companies).where(and(eq(companies.id, id), eq(companies.userId, userId))))[0];

export async function getCompany(userId: string, id: string) {
  const c = await getCompanyRow(userId, id);
  if (!c) return undefined;
  const people = await db.select(contactCols).from(contacts).leftJoin(companies, eq(companies.id, contacts.companyId)).where(and(eq(contacts.companyId, c.id), eq(contacts.userId, userId))).orderBy(asc(contacts.name));
  return { ...c, contacts: people };
}

function companyFields(b: unknown, partial: boolean): Err | { v: { name?: string; domain?: string } } {
  if (!isObj(b)) return { error: "Send a JSON object", status: 400 };
  const v: { name?: string; domain?: string } = {};
  if (!partial || "name" in b) { v.name = text(b.name); if (!v.name) return { error: "Name is required", status: 400 }; }
  if (!partial || "domain" in b) v.domain = text(b.domain);
  return { v };
}

export async function createCompany(userId: string, b: unknown): Promise<Err | { company: typeof companies.$inferSelect }> {
  const f = companyFields(b, false);
  if ("error" in f) return f;
  const [company] = await db.insert(companies).values({ ...f.v, userId } as typeof companies.$inferInsert).returning();
  return { company };
}

export async function updateCompany(userId: string, id: string, b: unknown): Promise<Err | { company: typeof companies.$inferSelect }> {
  const f = companyFields(b, true);
  if ("error" in f) return f;
  if (Object.keys(f.v).length) await db.update(companies).set(f.v).where(and(eq(companies.id, id), eq(companies.userId, userId)));
  const company = await getCompanyRow(userId, id);
  return company ? { company } : { error: "Not found", status: 404 };
}

export async function deleteCompany(userId: string, id: string) {
  return (await db.delete(companies).where(and(eq(companies.id, id), eq(companies.userId, userId))).returning({ id: companies.id })).length > 0;
}

/**
 * CSV import (header `name,email,phone,title,company`): rows with a blank name, or an email the person already has, are skipped;
 * a company is matched by name (case-insensitive) among the person's companies and created once if missing. All or nothing.
 */
export async function importContacts(userId: string, csv: unknown): Promise<Err | { imported: number; skipped: number }> {
  if (typeof csv !== "string" || !csv.trim()) return { error: "Choose a CSV file", status: 400 };
  if (csv.length > 2_000_000) return { error: "That file is too large", status: 400 };
  const [header, ...rows] = parseCsv(csv);
  const col = (n: string) => header.findIndex((h) => h.trim().toLowerCase() === n);
  const idx = { name: col("name"), email: col("email"), phone: col("phone"), title: col("title"), company: col("company") };
  if (idx.name < 0 || idx.email < 0) return { error: "The first row must be a header with name,email,phone,title,company", status: 400 };
  const cell = (r: string[], i: number) => (i < 0 ? "" : text(r[i]));
  return db.transaction(async (tx) => {
    const emails = new Set((await tx.select({ e: contacts.email }).from(contacts).where(eq(contacts.userId, userId))).map((r) => r.e).filter(Boolean));
    const byName = new Map((await tx.select().from(companies).where(eq(companies.userId, userId))).map((c) => [c.name.toLowerCase(), c.id]));
    let imported = 0, skipped = 0;
    for (const r of rows) {
      const name = cell(r, idx.name), email = cell(r, idx.email).toLowerCase();
      if (!name || (email && emails.has(email))) { skipped++; continue; }
      let companyId: string | null = null;
      const cName = cell(r, idx.company);
      if (cName) {
        companyId = byName.get(cName.toLowerCase()) ?? null;
        if (!companyId) {
          [{ id: companyId }] = await tx.insert(companies).values({ userId, name: cName }).returning({ id: companies.id });
          byName.set(cName.toLowerCase(), companyId);
        }
      }
      await tx.insert(contacts).values({ userId, companyId, name, email, phone: cell(r, idx.phone), title: cell(r, idx.title) });
      if (email) emails.add(email);
      imported++;
    }
    return { imported, skipped };
  });
}
