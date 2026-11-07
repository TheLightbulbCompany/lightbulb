import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { createOrg, listOrgs } from "@/lib/data";

export async function GET() {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  return NextResponse.json((await listOrgs(user.id)).map(({ id, name, slug }) => ({ id, name, slug })));
}

export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const r = await createOrg(user.id, await req.json().catch(() => null));
  if ("error" in r) return NextResponse.json({ error: r.error }, { status: r.status });
  const { id, name, slug } = r.org;
  return NextResponse.json({ id, name, slug }, { status: 201 });
}
