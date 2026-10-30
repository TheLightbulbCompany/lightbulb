import { and, desc, eq, inArray } from "drizzle-orm";
import { accounts, entries, entryLines } from "@/db/schema";
import { db } from "./db";
import { fmtCents } from "./money";

const text = (v: unknown) => String(v ?? "").trim();
type Err = { error: string; status: number };

/** Every query below is scoped to the signed-in person. */
export async function listAccounts(userId: string) {
  const rows = await db.select().from(accounts).where(eq(accounts.userId, userId));
  return rows.sort((a, b) => a.code.localeCompare(b.code, "en", { numeric: true }));
}

export async function createAccount(userId: string, b: any): Promise<Err | { account: typeof accounts.$inferSelect }> {
  const code = text(b?.code), name = text(b?.name), type = text(b?.type);
  if (!code) return { error: "Code is required", status: 400 };
  if (!name) return { error: "Name is required", status: 400 };
  if (type !== "debit" && type !== "credit") return { error: "Type must be debit or credit", status: 400 };
  const [account] = await db.insert(accounts).values({ userId, code, name, type }).onConflictDoNothing().returning();
  return account ? { account } : { error: `Code ${code} is already used`, status: 409 };
}

const entryJson = (e: typeof entries.$inferSelect) => ({ id: e.id, date: e.date, memo: e.memo, totalDebitCents: e.totalCents, totalCreditCents: e.totalCents });

export async function listEntries(userId: string) {
  const rows = await db.select().from(entries).where(eq(entries.userId, userId)).orderBy(desc(entries.date), desc(entries.createdAt));
  return rows.map(entryJson);
}

export async function getEntry(userId: string, id: string) {
  const [e] = await db.select().from(entries).where(and(eq(entries.id, id), eq(entries.userId, userId)));
  if (!e) return undefined;
  const lines = await db.select().from(entryLines).where(eq(entryLines.entryId, e.id)).orderBy(entryLines.position);
  return { ...entryJson(e), lines: lines.map((l) => ({ id: l.id, accountId: l.accountId, debitCents: l.debitCents, creditCents: l.creditCents })) };
}

const cents = (v: unknown) => (typeof v === "number" && Number.isSafeInteger(v) && v >= 0 && v <= 1e12 ? v : null);

/** Valid only when every line has exactly one positive side, its account is the person's, and debits equal credits above zero. */
export async function createEntry(userId: string, b: any): Promise<Err | { entry: NonNullable<Awaited<ReturnType<typeof getEntry>>> }> {
  const bad = (error: string): Err => ({ error, status: 400 });
  const date = text(b?.date);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || new Date(date + "T00:00:00Z").toISOString().slice(0, 10) !== date) return bad("Enter a valid date");
  if (!Array.isArray(b?.lines) || b.lines.length < 2) return bad("An entry needs at least two lines");
  let debit = 0, credit = 0;
  const lines: { accountId: string; debitCents: number; creditCents: number }[] = [];
  for (const l of b.lines) {
    const d = cents(l?.debitCents), c = cents(l?.creditCents);
    if (d === null || c === null || (d > 0) === (c > 0)) return bad("Each line needs a debit or a credit amount above zero, not both");
    debit += d; credit += c;
    lines.push({ accountId: text(l?.accountId), debitCents: d, creditCents: c });
  }
  if (debit !== credit) return bad(`Debits (${fmtCents(debit)}) and credits (${fmtCents(credit)}) must be equal`);
  const ids = [...new Set(lines.map((l) => l.accountId))];
  const own = await db.select({ id: accounts.id }).from(accounts).where(and(eq(accounts.userId, userId), inArray(accounts.id, ids)));
  if (own.length !== ids.length) return bad("Choose an account for every line");
  const id = await db.transaction(async (tx) => {
    const [e] = await tx.insert(entries).values({ userId, date, memo: text(b?.memo), totalCents: debit }).returning({ id: entries.id });
    await tx.insert(entryLines).values(lines.map((l, position) => ({ ...l, entryId: e.id, position })));
    return e.id;
  });
  return { entry: (await getEntry(userId, id))! };
}
