import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { createCompany, listCompanies } from "@/lib/data";

const shape = ({ id, name, domain }: { id: string; name: string; domain: string }) => ({ id, name, domain });

export async function GET() {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  return NextResponse.json((await listCompanies(user.id)).map(shape));
}

export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const r = await createCompany(user.id, await req.json().catch(() => null));
  return "error" in r ? NextResponse.json({ error: r.error }, { status: r.status }) : NextResponse.json(shape(r.company), { status: 201 });
}
