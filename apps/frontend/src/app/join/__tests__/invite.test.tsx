import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import InvitationPage from "../invite/page";

async function renderPage(search: Record<string, string>) {
  render(await InvitationPage({ searchParams: Promise.resolve(search) }));
}

describe("/join/invite", () => {
  it.each([
    [{ inviter: "Sam", workspace: "Acme" }, "Sam invited you to Acme."],
    [{ workspace: "Acme" }, "Your teammate invited you to Acme."],
    [{ inviter: "Sam" }, "Sam invited you to their workspace."],
  ])("names the inviter and the workspace (%o)", async (search, headline) => {
    await renderPage(search);
    expect(screen.getByRole("heading")).toHaveTextContent(headline);
  });

  it("hands Clerk's ticket on to /sign-up unchanged", async () => {
    await renderPage({ workspace: "Acme", inviter: "Sam", __clerk_ticket: "tk_1", __clerk_status: "sign_up" });
    expect(screen.getByRole("heading")).toHaveTextContent("Sam invited you to Acme.");
    const href = screen.getByRole("link", { name: "Sign up to join" }).getAttribute("href")!;
    expect(href.startsWith("/sign-up?")).toBe(true);
    expect(new URLSearchParams(href.split("?")[1]).get("__clerk_ticket")).toBe("tk_1");
  });

  it("sends an existing account to /sign-in", async () => {
    await renderPage({ __clerk_ticket: "tk_1", __clerk_status: "sign_in" });
    expect(screen.getByRole("link", { name: "Sign in to join" }).getAttribute("href")).toContain("/sign-in?");
    expect(screen.queryByRole("link", { name: "Sign up to join" })).not.toBeInTheDocument();
  });

  it("opens the app for an invitation already used", async () => {
    await renderPage({ __clerk_status: "complete" });
    expect(screen.getByRole("link", { name: "Open Lightbulb" })).toHaveAttribute("href", "/chat");
  });
});
