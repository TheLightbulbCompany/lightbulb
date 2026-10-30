import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { importContacts } from "@/lib/data";

// Body: { csv: "<the file's text>" }
export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const b = await req.json().catch(() => null);
  const r = await importContacts(user.id, b?.csv);
  return "error" in r ? NextResponse.json({ error: r.error }, { status: r.status }) : NextResponse.json(r);
}
