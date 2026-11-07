import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { listMembers, membership } from "@/lib/data";

export async function GET(_: Request, { params }: { params: Promise<{ orgId: string }> }) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const orgId = (await params).orgId;
  if (!(await membership(user.id, orgId))) return NextResponse.json({ error: "Not found" }, { status: 404 });
  return NextResponse.json(await listMembers(orgId));
}
