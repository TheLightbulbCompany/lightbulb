import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { listOrders, placeOrder } from "@/lib/data";

export async function GET() {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  return NextResponse.json(await listOrders(user.id));
}

export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const r = await placeOrder(user.id, await req.json().catch(() => null));
  return "error" in r ? NextResponse.json({ error: r.error }, { status: r.status }) : NextResponse.json(r.order, { status: 201 });
}
