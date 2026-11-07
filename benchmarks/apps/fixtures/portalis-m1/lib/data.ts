import { and, asc, eq } from "drizzle-orm";
import { orgMembers, orgs, users } from "@/db/schema";
import { db } from "./db";

const text = (v: unknown) => String(v ?? "").trim();

/** The caller's organizations. */
export const listOrgs = (userId: string) =>
  db.select({ id: orgs.id, name: orgs.name, slug: orgs.slug, description: orgs.description })
    .from(orgMembers).innerJoin(orgs, eq(orgs.id, orgMembers.orgId))
    .where(eq(orgMembers.userId, userId)).orderBy(asc(orgs.createdAt));

/** The organization and the caller's role in it, or undefined when they are not a member. Every org page and request goes through this. */
export async function membership(userId: string, orgId: string) {
  const [row] = await db.select({ org: orgs, role: orgMembers.role })
    .from(orgMembers).innerJoin(orgs, eq(orgs.id, orgMembers.orgId))
    .where(and(eq(orgMembers.userId, userId), eq(orgMembers.orgId, orgId)));
  return row;
}

export const listMembers = (orgId: string) =>
  db.select({ userId: users.id, email: users.email, name: users.name, role: orgMembers.role })
    .from(orgMembers).innerJoin(users, eq(users.id, orgMembers.userId))
    .where(eq(orgMembers.orgId, orgId)).orderBy(asc(orgMembers.createdAt));

export async function createOrg(userId: string, b: any): Promise<{ error: string; status: number } | { org: typeof orgs.$inferSelect }> {
  const name = text(b?.name), slug = text(b?.slug).toLowerCase();
  if (!name) return { error: "Name is required", status: 400 };
  if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(slug)) return { error: "Slug may only use lowercase letters, numbers and dashes", status: 400 };
  return db.transaction(async (tx) => {
    const [org] = await tx.insert(orgs).values({ name, slug }).onConflictDoNothing().returning();
    if (!org) return { error: "That slug is already taken", status: 409 };
    await tx.insert(orgMembers).values({ orgId: org.id, userId, role: "org_admin" });
    return { org };
  });
}

export async function updateOrg(orgId: string, b: any): Promise<{ error: string; status: number } | { org: typeof orgs.$inferSelect }> {
  const name = text(b?.name);
  if (!name) return { error: "Name is required", status: 400 };
  const [org] = await db.update(orgs).set({ name, description: text(b?.description) }).where(eq(orgs.id, orgId)).returning();
  return { org };
}
