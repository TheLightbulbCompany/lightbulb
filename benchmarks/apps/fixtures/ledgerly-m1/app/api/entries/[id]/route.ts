import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { getEntry } from "@/lib/data";

export async function GET(_: Request, { params }: { params: Promise<{ id: string }> }) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const e = await getEntry(user.id, (await params).id);
  return e ? NextResponse.json(e) : NextResponse.json({ error: "Not found" }, { status: 404 });
}
