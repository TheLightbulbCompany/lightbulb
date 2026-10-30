import { randomUUID } from "node:crypto";
import { integer, pgTable, text, timestamp } from "drizzle-orm/pg-core";

const id = () => text("id").primaryKey().$defaultFn(() => randomUUID());
const utc = (name: string) => timestamp(name, { withTimezone: true }); // every stored time is an absolute instant (UTC)

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

// Practitioners and services are shared by everybody.
export const practitioners = pgTable("practitioners", {
  id: id(),
  name: text("name").notNull(),
  specialty: text("specialty").notNull().default(""),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

export const services = pgTable("services", {
  id: id(),
  name: text("name").notNull(),
  durationMinutes: integer("duration_minutes").notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

export const bookings = pgTable("bookings", {
  id: id(),
  userId: text("user_id").notNull().references(() => users.id, { onDelete: "cascade" }), // the patient
  practitionerId: text("practitioner_id").notNull().references(() => practitioners.id),
  serviceId: text("service_id").notNull().references(() => services.id),
  startAt: utc("start_at").notNull(),
  endAt: utc("end_at").notNull(), // start + the service length, worked out by the server
  status: text("status").notNull().default("booked"), // booked | cancelled
  createdAt: timestamp("created_at").notNull().defaultNow(),
});
