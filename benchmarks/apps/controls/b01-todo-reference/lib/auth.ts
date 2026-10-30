import { createHash, randomBytes, scryptSync, timingSafeEqual } from "node:crypto";
import { and, eq, gt } from "drizzle-orm";
import { cookies, headers } from "next/headers";
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
  if (!salt || !hash) return false; // malformed stored hash: no match, no throw
  const want = Buffer.from(hash, "hex");
  if (!want.length) return false;
  return timingSafeEqual(scryptSync(pw, Buffer.from(salt, "hex"), want.length), want);
}
/** Burn the same scrypt time as a real check, so an unknown email is not faster than a wrong password. */
export const DUMMY_HASH = hashPassword("not-a-real-password");

/** Start a session for `userId` and set its httpOnly cookie. */
export async function startSession(userId: string) {
  const token = randomBytes(32).toString("hex");
  await db.insert(sessions).values({ tokenHash: sha(token), userId, expiresAt: new Date(Date.now() + WEEK * 1000) });
  // `secure` only over https in production: the eval serves the built app over plain http on localhost, where a Secure cookie would never come back.
  const secure = process.env.NODE_ENV === "production" && (await headers()).get("x-forwarded-proto") === "https";
  (await cookies()).set(COOKIE, token, { httpOnly: true, sameSite: "lax", path: "/", maxAge: WEEK, secure });
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
