import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { createAccount, listAccounts } from "@/lib/data";

export async function GET() {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  return NextResponse.json((await listAccounts(user.id)).map(({ id, code, name, type }) => ({ id, code, name, type })));
}

export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const r = await createAccount(user.id, await req.json().catch(() => null));
  if ("error" in r) return NextResponse.json({ error: r.error }, { status: r.status });
  const { id, code, name, type } = r.account;
  return NextResponse.json({ id, code, name, type }, { status: 201 });
}
