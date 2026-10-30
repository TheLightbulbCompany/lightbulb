import { NextResponse } from "next/server";
import { users } from "@/db/schema";
import { hashPassword, startSession } from "@/lib/auth";
import { db } from "@/lib/db";

export async function POST(req: Request) {
  const b = await req.json().catch(() => ({}));
  const name = String(b.name ?? "").trim();
  const email = String(b.email ?? "").trim().toLowerCase();
  const password = String(b.password ?? "");
  if (!name) return NextResponse.json({ error: "Name is required" }, { status: 400 });
  if (!/^[^@\s]+@[^@\s]+$/.test(email)) return NextResponse.json({ error: "Enter a valid email" }, { status: 400 });
  if (password.length < 8) return NextResponse.json({ error: "Password must be at least 8 characters" }, { status: 400 });
  const [user] = await db.insert(users).values({ name, email, passwordHash: hashPassword(password) }).onConflictDoNothing().returning({ id: users.id });
  if (!user) return NextResponse.json({ error: "That email is already registered" }, { status: 409 });
  await startSession(user.id);
  return NextResponse.json({ id: user.id });
}
