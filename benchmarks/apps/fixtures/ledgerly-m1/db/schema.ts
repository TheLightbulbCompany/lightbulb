import { randomUUID } from "node:crypto";
import { bigint, date, integer, pgTable, text, timestamp, unique } from "drizzle-orm/pg-core";

const id = () => text("id").primaryKey().$defaultFn(() => randomUUID());
const cents = (name: string) => bigint(name, { mode: "number" }).notNull(); // whole cents, never floats

export const users = pgTable("users", {
  id: id(),
  name: text("name").notNull(),
  email: text("email").notNull().unique(),
  passwordHash: text("password_hash").notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

// The cookie holds a random token; only its sha256 is stored here.
export const sessions = pgTable("sessions", {
  tokenHash: text("token_hash").primaryKey(),
  userId: text("user_id").notNull().references(() => users.id, { onDelete: "cascade" }),
  expiresAt: timestamp("expires_at").notNull(),
});

export const accounts = pgTable("accounts", {
  id: id(),
  userId: text("user_id").notNull().references(() => users.id, { onDelete: "cascade" }),
  code: text("code").notNull(),
  name: text("name").notNull(),
  type: text("type").notNull(), // debit | credit: the normal side
  createdAt: timestamp("created_at").notNull().defaultNow(),
}, (t) => [unique("accounts_user_code").on(t.userId, t.code)]);

export const entries = pgTable("entries", {
  id: id(),
  userId: text("user_id").notNull().references(() => users.id, { onDelete: "cascade" }),
  date: date("date", { mode: "string" }).notNull(), // a calendar date: kept as the YYYY-MM-DD string, never a Date
  memo: text("memo").notNull().default(""),
  totalCents: cents("total_cents"), // total debits = total credits
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

export const entryLines = pgTable("entry_lines", {
  id: id(),
  entryId: text("entry_id").notNull().references(() => entries.id, { onDelete: "cascade" }),
  accountId: text("account_id").notNull().references(() => accounts.id),
  debitCents: cents("debit_cents"),
  creditCents: cents("credit_cents"),
  position: integer("position").notNull(),
});
