import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";
import { getOrder } from "@/lib/data";

export async function GET(_: Request, { params }: { params: Promise<{ id: string }> }) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  const order = await getOrder(user.id, (await params).id);
  return order ? NextResponse.json(order) : NextResponse.json({ error: "Not found" }, { status: 404 });
}
