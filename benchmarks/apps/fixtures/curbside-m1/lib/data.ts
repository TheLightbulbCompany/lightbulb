import { and, desc, eq, inArray } from "drizzle-orm";
import { menuItems, orderLines, orders, restaurants } from "@/db/schema";
import { db } from "./db";

export const getRestaurant = async (id: string) => (await db.select().from(restaurants).where(eq(restaurants.id, id)))[0];
export const menuOf = (restaurantId: string) => db.select().from(menuItems).where(eq(menuItems.restaurantId, restaurantId)).orderBy(menuItems.createdAt);

const text = (v: unknown) => String(v ?? "").trim();
export function parseRestaurant(b: any): { error: string } | { name: string; cuisine: string; address: string } {
  const r = { name: text(b?.name), cuisine: text(b?.cuisine), address: text(b?.address) };
  return !r.name ? { error: "Name is required" } : !r.cuisine ? { error: "Cuisine is required" } : !r.address ? { error: "Address is required" } : r;
}
export function parseMenuItem(b: any): { error: string } | { name: string; description: string; priceCents: number } {
  const name = text(b?.name);
  if (!name) return { error: "Name is required" };
  const priceCents = Number(b?.priceCents);
  if (!Number.isSafeInteger(priceCents) || priceCents < 0) return { error: "Price must be a whole number of cents" };
  return { name, description: text(b?.description), priceCents };
}

/** Every order query is scoped to the customer. */
export const listOrders = (userId: string) =>
  db.select().from(orders).where(eq(orders.userId, userId)).orderBy(desc(orders.createdAt));

/** One of the customer's orders with its restaurant name and lines, or undefined (also for someone else's order). */
export async function getOrder(userId: string, id: string) {
  const [o] = await db.select({ order: orders, restaurantName: restaurants.name }).from(orders)
    .innerJoin(restaurants, eq(restaurants.id, orders.restaurantId))
    .where(and(eq(orders.id, id), eq(orders.userId, userId)));
  if (!o) return undefined;
  return { ...o.order, restaurantName: o.restaurantName, lines: await db.select().from(orderLines).where(eq(orderLines.orderId, id)) };
}

/** Place an order: prices come from the database, whatever the client sent. */
export async function placeOrder(userId: string, b: any): Promise<{ error: string; status: number } | { order: typeof orders.$inferSelect }> {
  const restaurant = await getRestaurant(String(b?.restaurantId ?? ""));
  if (!restaurant) return { error: "Restaurant not found", status: 404 };
  const want = new Map<string, number>();
  for (const i of Array.isArray(b?.items) ? b.items : []) {
    const q = Number(i?.quantity);
    if (typeof i?.menuItemId !== "string" || !Number.isInteger(q) || q < 1 || q > 99) return { error: "Each item needs a menuItemId and a quantity from 1 to 99", status: 400 };
    want.set(i.menuItemId, (want.get(i.menuItemId) ?? 0) + q);
  }
  if (!want.size) return { error: "Add at least one item", status: 400 };
  const found = await db.select().from(menuItems).where(and(eq(menuItems.restaurantId, restaurant.id), inArray(menuItems.id, [...want.keys()])));
  if (found.length !== want.size) return { error: "Every item must be on this restaurant's menu", status: 400 };
  const lines = found.map((m) => ({ menuItemId: m.id, name: m.name, unitCents: m.priceCents, quantity: want.get(m.id)!, lineCents: m.priceCents * want.get(m.id)! }));
  const subtotal = lines.reduce((s, l) => s + l.lineCents, 0);
  const order = await db.transaction(async (tx) => {
    const [o] = await tx.insert(orders).values({ userId, restaurantId: restaurant.id, subtotalCents: subtotal, totalCents: subtotal }).returning(); // total = subtotal for now
    await tx.insert(orderLines).values(lines.map((l) => ({ ...l, orderId: o.id })));
    return o;
  });
  return { order };
}
