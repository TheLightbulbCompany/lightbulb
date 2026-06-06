import { NextRequest, NextResponse } from "next/server";
import { checkout } from "@/lib/marketplace/api";

export async function POST(req: NextRequest) {
  const body = await req.json();
  const origin = new URL(req.url).origin;
  try {
    // {CHECKOUT_SESSION_ID} is a Stripe placeholder — Stripe substitutes it
    // for the real session id when redirecting on completion. The
    // /deploy-success page uses session_id presence to differentiate paid
    // returns from free deploys, and fires the deploy on mount.
    const successUrl =
      `${origin}/deploy-success?listing_id=${encodeURIComponent(body.listing_id)}` +
      `&session_id={CHECKOUT_SESSION_ID}`;
    const result = await checkout({
      listingId: body.listing_id,
      successUrl,
      cancelUrl: `${origin}/listing/${body.listing_id}`,
      jwt: body.jwt,
      email: body.email,
    });
    return NextResponse.json(result);
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "checkout failed" },
      { status: 500 }
    );
  }
}
