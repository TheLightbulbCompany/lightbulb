import { asc } from "drizzle-orm";
import { NextResponse } from "next/server";
import { services } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { parseService } from "@/lib/data";
import { db } from "@/lib/db";

export async function GET() {
  if (!(await currentUser())) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  return NextResponse.json(await db.select().from(services).orderBy(asc(services.createdAt)));
}

export async function POST(req: Request) {
  if (!(await currentUser())) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const s = parseService(await req.json().catch(() => null));
  if ("error" in s) return NextResponse.json(s, { status: 400 });
  const [row] = await db.insert(services).values(s).returning();
  return NextResponse.json(row, { status: 201 });
}
