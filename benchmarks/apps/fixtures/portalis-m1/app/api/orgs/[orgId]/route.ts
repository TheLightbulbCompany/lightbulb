import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { membership, updateOrg } from "@/lib/data";

type Ctx = { params: Promise<{ orgId: string }> };
const no = (status: number, error: string) => NextResponse.json({ error }, { status });
const shape = ({ id, name, slug, description }: { id: string; name: string; slug: string; description: string }) => ({ id, name, slug, description });

export async function GET(_: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const m = await membership(user.id, (await params).orgId);
  return m ? NextResponse.json(shape(m.org)) : no(404, "Not found"); // a non-member learns nothing, not even that it exists
}

export async function PATCH(req: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const m = await membership(user.id, (await params).orgId);
  if (!m) return no(404, "Not found");
  const r = await updateOrg(m.org.id, await req.json().catch(() => null));
  return "error" in r ? no(r.status, r.error) : NextResponse.json(shape(r.org));
}
