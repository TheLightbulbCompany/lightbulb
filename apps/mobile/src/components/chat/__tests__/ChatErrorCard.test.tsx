import { render, screen } from "@testing-library/react-native";

import { ChatErrorCard, describeChatError } from "../ChatErrorCard";

// This card used to carry an "Upgrade on isol8.co" button and to prefer the
// backend's copy over the local fallback. Both were removed for App Store
// guideline 3.1.3(f) — see lib/purchaseCta. The assertions below are the
// inverse of what they were on purpose; a reviewer restoring either behaviour
// should fail here.
describe("describeChatError", () => {
  it("maps the free-tier gate codes to local, CTA-free copy", () => {
    const cap = describeChatError("trial_daily_cap");
    expect(cap?.title).toBe("Daily free limit reached");
    expect(cap?.description).toBe(
      "You've used today's free allowance. It resets tomorrow.",
    );

    const expired = describeChatError("trial_expired");
    expect(expired?.title).toBe("Your free trial has ended");
    expect(expired?.description).not.toMatch(/upgrade|plan|subscribe/i);
  });

  it("returns null for unmapped or missing codes", () => {
    expect(describeChatError("timeout")).toBeNull();
    expect(describeChatError("subscription_inactive")).toBeNull();
    expect(describeChatError(undefined)).toBeNull();
  });
});

describe("ChatErrorCard", () => {
  // The backend's own copy for this code ends "Upgrade to keep going". The
  // whole point of the divergence is that it must not reach the screen.
  it("renders the structured card without any purchase call to action", async () => {
    await render(
      <ChatErrorCard
        code="trial_daily_cap"
        message="You've used today's free allowance — Upgrade to keep going."
      />,
    );

    expect(screen.getByText("Daily free limit reached")).toBeTruthy();
    expect(screen.queryByText(/upgrade/i)).toBeNull();
    expect(screen.queryByText(/isol8\.co/)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders the plain message card for uncoded errors", async () => {
    await render(<ChatErrorCard message="Send failed" />);
    expect(screen.getByText("Send failed")).toBeTruthy();
    expect(screen.queryByText(/on Lightbulb AI\.co/)).toBeNull();
  });
});
