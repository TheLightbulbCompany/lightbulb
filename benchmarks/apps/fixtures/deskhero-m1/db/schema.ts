import { randomUUID } from "node:crypto";
import { pgTable, text, timestamp } from "drizzle-orm/pg-core";

const id = () => text("id").primaryKey().$defaultFn(() => randomUUID());

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

export const tickets = pgTable("tickets", {
  id: id(),
  userId: text("user_id").notNull().references(() => users.id, { onDelete: "cascade" }), // the creator
  subject: text("subject").notNull(),
  body: text("body").notNull().default(""),
  priority: text("priority").notNull().default("medium"), // low | medium | high
  status: text("status").notNull().default("open"), // open | closed
  createdAt: timestamp("created_at").notNull().defaultNow(),
});
