import { eq } from "drizzle-orm";
import { NextResponse } from "next/server";
import { users } from "@/db/schema";
import { checkPassword, DUMMY_HASH, startSession } from "@/lib/auth";
import { db } from "@/lib/db";

export async function POST(req: Request) {
  const b = await req.json().catch(() => ({}));
  const email = String(b.email ?? "").trim().toLowerCase();
  const [user] = await db.select().from(users).where(eq(users.email, email));
  const ok = checkPassword(String(b.password ?? ""), user?.passwordHash ?? DUMMY_HASH);
  if (!user || !ok) return NextResponse.json({ error: "Wrong email or password" }, { status: 401 });
  await startSession(user.id);
  return NextResponse.json({ id: user.id });
}
