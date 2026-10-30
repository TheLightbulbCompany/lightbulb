import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { deleteContact, getContact, updateContact } from "@/lib/data";

type Ctx = { params: Promise<{ id: string }> };
const no = (status: number, error: string) => NextResponse.json({ error }, { status });

export async function GET(_: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const c = await getContact(user.id, (await params).id);
  return c ? NextResponse.json(c) : no(404, "Not found");
}

export async function PATCH(req: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  const r = await updateContact(user.id, (await params).id, await req.json().catch(() => null));
  return "error" in r ? no(r.status, r.error) : NextResponse.json(r.contact);
}

export async function DELETE(_: Request, { params }: Ctx) {
  const user = await currentUser();
  if (!user) return no(401, "Not signed in");
  return (await deleteContact(user.id, (await params).id)) ? NextResponse.json({ ok: true }) : no(404, "Not found");
}
