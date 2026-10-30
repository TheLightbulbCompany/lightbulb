import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { createEntry, listEntries } from "@/lib/data";

export async function GET() {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  return NextResponse.json(await listEntries(user.id));
}

export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const r = await createEntry(user.id, await req.json().catch(() => null));
  return "error" in r ? NextResponse.json({ error: r.error }, { status: r.status }) : NextResponse.json(r.entry, { status: 201 });
}
