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
    const forwarded = new URLSearchParams(href.split("?")[1]);
    expect(forwarded.get("__clerk_ticket")).toBe("tk_1");
    // The names stay on this page; /sign-up gets only what Clerk's form needs.
    expect(forwarded.has("inviter")).toBe(false);
    expect(forwarded.has("workspace")).toBe(false);
  });

  it("forwards a full-length ticket untruncated", async () => {
    const ticket = "x".repeat(600);
    await renderPage({ __clerk_ticket: ticket, __clerk_status: "sign_up" });
    const href = screen.getByRole("link", { name: "Sign up to join" }).getAttribute("href")!;
    expect(new URLSearchParams(href.split("?")[1]).get("__clerk_ticket")).toBe(ticket);
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

  it("names the decision and sends the new teammate to Activity (Needs you) after sign-up", async () => {
    await renderPage({ inviter: "Ada", approval: "ap-1", decision: "Start outreach?", __clerk_ticket: "tk_1" });
    expect(screen.getByRole("heading")).toHaveTextContent("Ada asked you to decide: Start outreach?");
    const href = screen.getByRole("link", { name: "Sign up to join" }).getAttribute("href")!;
    expect(new URLSearchParams(href.split("?")[1]).get("redirect_url")).toBe("/chat?panel=activity");
  });

  it("ignores an approval id that is not an id", async () => {
    await renderPage({ approval: "../x", decision: "x", __clerk_ticket: "tk_1" });
    const href = screen.getByRole("link", { name: "Sign up to join" }).getAttribute("href")!;
    expect(new URLSearchParams(href.split("?")[1]).get("redirect_url")).toBeNull();
  });
});
