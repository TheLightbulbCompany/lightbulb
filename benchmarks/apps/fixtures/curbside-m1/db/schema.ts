import { randomUUID } from "node:crypto";
import { integer, pgTable, text, timestamp } from "drizzle-orm/pg-core";

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

export const restaurants = pgTable("restaurants", {
  id: id(),
  ownerId: text("owner_id").notNull().references(() => users.id, { onDelete: "cascade" }),
  name: text("name").notNull(),
  cuisine: text("cuisine").notNull(),
  address: text("address").notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

export const menuItems = pgTable("menu_items", {
  id: id(),
  restaurantId: text("restaurant_id").notNull().references(() => restaurants.id, { onDelete: "cascade" }),
  name: text("name").notNull(),
  description: text("description").notNull().default(""),
  priceCents: integer("price_cents").notNull(), // whole cents, never a decimal
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

export const orders = pgTable("orders", {
  id: id(),
  userId: text("user_id").notNull().references(() => users.id, { onDelete: "cascade" }), // the customer
  restaurantId: text("restaurant_id").notNull().references(() => restaurants.id),
  status: text("status").notNull().default("placed"), // placed | cancelled
  paymentStatus: text("payment_status").notNull().default("unpaid"), // unpaid | paid
  subtotalCents: integer("subtotal_cents").notNull(),
  totalCents: integer("total_cents").notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

// Name and unit price are copied from the menu item when the order is placed.
export const orderLines = pgTable("order_lines", {
  id: id(),
  orderId: text("order_id").notNull().references(() => orders.id, { onDelete: "cascade" }),
  menuItemId: text("menu_item_id").notNull().references(() => menuItems.id),
  name: text("name").notNull(),
  unitCents: integer("unit_cents").notNull(),
  quantity: integer("quantity").notNull(),
  lineCents: integer("line_cents").notNull(),
});
