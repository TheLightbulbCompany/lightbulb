import { eq } from "drizzle-orm";
import { NextResponse } from "next/server";
import { practitioners } from "@/db/schema";
import { currentUser } from "@/lib/auth";
import { parsePractitioner } from "@/lib/data";
import { db } from "@/lib/db";

type Ctx = { params: Promise<{ id: string }> };
const no = (status: number, error: string) => NextResponse.json({ error }, { status });

export async function PATCH(req: Request, { params }: Ctx) {
  if (!(await currentUser())) return no(401, "Not signed in");
  const v = parsePractitioner(await req.json().catch(() => null));
  if ("error" in v) return no(400, v.error);
  const [row] = await db.update(practitioners).set(v).where(eq(practitioners.id, (await params).id)).returning();
  return row ? NextResponse.json(row) : no(404, "Not found");
}

export async function DELETE(_: Request, { params }: Ctx) {
  if (!(await currentUser())) return no(401, "Not signed in");
  try {
    const [row] = await db.delete(practitioners).where(eq(practitioners.id, (await params).id)).returning({ id: practitioners.id });
    return row ? NextResponse.json({ ok: true }) : no(404, "Not found");
  } catch {
    return no(409, "That practitioner has bookings"); // bookings keep a reference to it
  }
}
