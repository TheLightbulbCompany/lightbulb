import { fireEvent, render, screen } from "@testing-library/react-native";
import { Linking } from "react-native";

import { ChatErrorCard, describeChatError } from "../ChatErrorCard";

describe("describeChatError", () => {
  it("maps the free-tier gate codes to tailored copy with an upgrade action", () => {
    const cap = describeChatError("trial_daily_cap", "backend copy");
    expect(cap?.title).toBe("Daily free limit reached");
    expect(cap?.description).toBe("backend copy");
    expect(cap?.action.url).toBe("https://isol8.co/settings?panel=billing");

    const expired = describeChatError("trial_expired", "");
    expect(expired?.title).toBe("Your free trial has ended");
    // Empty backend message falls back to local copy.
    expect(expired?.description).toContain("Pick a plan");
  });

  it("returns null for unmapped or missing codes", () => {
    expect(describeChatError("timeout", "msg")).toBeNull();
    expect(describeChatError("subscription_inactive", "msg")).toBeNull();
    expect(describeChatError(undefined, "msg")).toBeNull();
  });
});

describe("ChatErrorCard", () => {
  it("renders the structured card and opens the billing panel on press", async () => {
    const openURL = jest
      .spyOn(Linking, "openURL")
      .mockResolvedValue(undefined as never);
    await render(
      <ChatErrorCard code="trial_daily_cap" message="You've used today's free allowance." />,
    );
    expect(screen.getByText("Daily free limit reached")).toBeTruthy();
    fireEvent.press(screen.getByText("Upgrade on isol8.co"));
    expect(openURL).toHaveBeenCalledWith("https://isol8.co/settings?panel=billing");
    openURL.mockRestore();
  });

  it("renders the plain message card for uncoded errors", async () => {
    await render(<ChatErrorCard message="Send failed" />);
    expect(screen.getByText("Send failed")).toBeTruthy();
    expect(screen.queryByText(/on isol8\.co/)).toBeNull();
  });
});
