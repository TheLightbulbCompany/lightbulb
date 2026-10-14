// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import React from "react";

const replace = vi.fn();
let search = "";

// Identity-stable, like the real useRouter() — a fresh literal per call makes
// any effect that depends on the router re-fire on every render.
const router = { replace };
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => new URLSearchParams(search),
}));

import ConnectionsPage from "../connections/page";

// /settings/connections is the fixed landing spot for OAuth consent (via
// /oauth/done) and for the backend's STRIPE_COMPANY_RETURN_URL, a CDK-set env
// var on the deployed service. It forwards to the Settings page's Connections
// section. If this forward breaks, every connect flow dead-ends here.
describe("/settings/connections redirector", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    search = "";
  });

  it("forwards a bare visit to the Connections section", () => {
    render(<ConnectionsPage />);
    expect(replace).toHaveBeenCalledWith("/settings?panel=connections");
  });

  it("carries an OAuth return's params through so the section can announce it", () => {
    search = "?connected=Notion";
    render(<ConnectionsPage />);
    expect(replace).toHaveBeenCalledWith("/settings?connected=Notion&panel=connections");
  });

  it("carries a Stripe return's params through", () => {
    search = "?stripe=return&stripe_connect=connected";
    render(<ConnectionsPage />);
    expect(replace).toHaveBeenCalledWith(
      "/settings?stripe=return&stripe_connect=connected&panel=connections",
    );
  });
});
