import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { createContact, listContacts } from "@/lib/data";

export async function GET() {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  return NextResponse.json(await listContacts(user.id));
}

export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const r = await createContact(user.id, await req.json().catch(() => null));
  return "error" in r ? NextResponse.json({ error: r.error }, { status: r.status }) : NextResponse.json(r.contact, { status: 201 });
}
