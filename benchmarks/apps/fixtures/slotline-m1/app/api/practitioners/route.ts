import { asc } from "drizzle-orm";
import { NextResponse } from "next/server";
import { practitioners } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { parsePractitioner } from "@/lib/data";
import { db } from "@/lib/db";

export async function GET() {
  if (!(await currentUser())) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  return NextResponse.json(await db.select().from(practitioners).orderBy(asc(practitioners.createdAt)));
}

export async function POST(req: Request) {
  if (!(await currentUser())) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const p = parsePractitioner(await req.json().catch(() => null));
  if ("error" in p) return NextResponse.json(p, { status: 400 });
  const [row] = await db.insert(practitioners).values(p).returning();
  return NextResponse.json(row, { status: 201 });
}
