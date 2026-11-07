import { NextResponse } from "next/server";
import { currentUser } from "@/lib/auth";

export async function GET() {
  const user = await currentUser();
  return user ? NextResponse.json(user) : NextResponse.json({ error: "Not signed in" }, { status: 401 });
}
