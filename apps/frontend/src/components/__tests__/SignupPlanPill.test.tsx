import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SignupPlanPill } from "../SignupPlanPill";

function setSearch(search: string) {
  window.history.replaceState({}, "", `/sign-up${search}`);
}

afterEach(() => setSearch(""));

describe("SignupPlanPill", () => {
  it("promises the free tier on a personal signup, which actually gets one", async () => {
    setSearch("");
    render(<SignupPlanPill />);
    expect(await screen.findByText("Free for 3 days")).toBeInTheDocument();
    expect(screen.getByText(/no credit card/i)).toBeInTheDocument();
  });

  it("does NOT promise it on a team signup, which gets no free tier at all", async () => {
    // `ensure_personal_trial_default` is personal-only, and the team-intent
    // cookie suppresses the poll that would create a trial_local row. Team
    // billing is $50 + $20/seat at checkout. The unconditional free-trial copy
    // this component replaced was a specific, false promise to every arrival
    // from the landing page's "Create a team" CTA.
    setSearch("?intent=team");
    render(<SignupPlanPill />);
    await waitFor(() => expect(screen.getByText("Per-seat billing")).toBeInTheDocument());
    expect(screen.queryByText(/no credit card/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/free for 3 days/i)).not.toBeInTheDocument();
  });

  it("ignores an unrelated intent value", async () => {
    setSearch("?intent=something-else");
    render(<SignupPlanPill />);
    expect(await screen.findByText("Free for 3 days")).toBeInTheDocument();
  });
});
