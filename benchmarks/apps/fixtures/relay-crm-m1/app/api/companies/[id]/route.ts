import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { deleteCompany, getCompany, updateCompany } from "@/lib/data";

type Ctx = { params: Promise<{ id: string }> };
const no = (status: number, error: string) => NextResponse.json({ error }, { status });
const shape = ({ id, name, domain }: { id: string; name: string; domain: string }) => ({ id, name, domain });

export async function GET(_: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const c = await getCompany(user.id, (await params).id);
  return c ? NextResponse.json({ ...shape(c), contacts: c.contacts }) : no(404, "Not found");
}

export async function PATCH(req: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const r = await updateCompany(user.id, (await params).id, await req.json().catch(() => null));
  return "error" in r ? no(r.status, r.error) : NextResponse.json(shape(r.company));
}

export async function DELETE(_: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  return (await deleteCompany(user.id, (await params).id)) ? NextResponse.json({ ok: true }) : no(404, "Not found");
}
