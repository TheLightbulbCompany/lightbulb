import { createHash, randomBytes, scryptSync, timingSafeEqual } from "node:crypto";
import { and, eq, gt } from "drizzle-orm";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { sessions, users } from "@/db/schema";
import { db } from "./db";

const COOKIE = "session";
const WEEK = 7 * 24 * 3600;
const sha = (s: string) => createHash("sha256").update(s).digest("hex");

export function hashPassword(pw: string): string {
  const salt = randomBytes(16);
  return `${salt.toString("hex")}:${scryptSync(pw, salt, 64).toString("hex")}`;
}
export function checkPassword(pw: string, stored: string): boolean {
  const [salt, hash] = stored.split(":");
  const want = Buffer.from(hash, "hex");
  const got = scryptSync(pw, Buffer.from(salt, "hex"), want.length);
  return timingSafeEqual(got, want);
}

/** Start a session for `userId` and set its httpOnly cookie. */
export async function startSession(userId: string) {
  const token = randomBytes(32).toString("hex");
  await db.insert(sessions).values({ tokenHash: sha(token), userId, expiresAt: new Date(Date.now() + WEEK * 1000) });
  (await cookies()).set(COOKIE, token, { httpOnly: true, sameSite: "lax", path: "/", maxAge: WEEK });
}

export async function endSession() {
  const jar = await cookies();
  const token = jar.get(COOKIE)?.value;
  if (token) await db.delete(sessions).where(eq(sessions.tokenHash, sha(token)));
  jar.delete(COOKIE);
}

/** The signed-in user, or null. */
export async function currentUser() {
  const token = (await cookies()).get(COOKIE)?.value;
  if (!token) return null;
  const [row] = await db
    .select({ id: users.id, name: users.name, email: users.email })
    .from(sessions)
    .innerJoin(users, eq(users.id, sessions.userId))
    .where(and(eq(sessions.tokenHash, sha(token)), gt(sessions.expiresAt, new Date())));
  return row ?? null;
}

/** For pages: the signed-in user, or a redirect to sign-in. */
export async function requireUser() {
  const user = await currentUser();
  if (!user) redirect("/auth/sign-in");
  return user;
}
