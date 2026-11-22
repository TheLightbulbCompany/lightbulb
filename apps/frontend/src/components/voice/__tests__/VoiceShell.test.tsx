import { render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ path: "/voice/settings", replace: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => nav.path, useRouter: () => ({ replace: nav.replace }) }));
const toast = vi.hoisted(() => ({ notify: vi.fn(), notifySuccess: vi.fn() }));
vi.mock("@/lib/notify", () => toast);
const org = vi.hoisted(() => ({ userId: "u" }));
// No organization hook or component: the shell reads only who is signed in.
vi.mock("@clerk/nextjs", () => ({ UserButton: () => null, useAuth: () => ({ userId: org.userId }) }));
vi.mock("../TalkSessionProvider", () => ({ TalkSessionProvider: ({ children }: { children: React.ReactNode }) => <div data-testid="talk-owner">{children}</div> }));
vi.mock("@/components/huddle/HuddleProvider", () => ({ HuddleProvider: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
const questions = vi.hoisted(() => ({ list: [] as { id: string }[] }));
const agent = vi.hoisted(() => ({ setup: false, config: { business: "Acme" } as { business?: string; live?: boolean } | undefined, provision: vi.fn() }));
vi.mock("../data", () => ({
  LIGHTBULB: "Lightbulb", useQuestions: () => ({ data: questions.list }), useGoLiveAfterPayment: () => {},
  useProvisionOnOpen: agent.provision,
  useAgentConfig: () => ({ data: agent.config, isNew: false }), needsOnboarding: () => agent.setup,
}));

import { VoiceShell } from "../VoiceShell";

it("Settings rail item opens /voice/settings", () => {
  render(<VoiceShell>x</VoiceShell>);
  const settings = screen.getByRole("link", { name: "Settings" });
  expect(settings).toHaveAttribute("href", "/voice/settings");
  expect(settings).toHaveAttribute("aria-current", "page");
});

it("Meetings rail item opens /voice/meetings", () => {
  nav.path = "/voice/meetings";
  render(<VoiceShell>x</VoiceShell>);
  const meetings = screen.getByRole("link", { name: "Meetings" });
  expect(meetings).toHaveAttribute("href", "/voice/meetings");
  expect(meetings).toHaveAttribute("aria-current", "page");
  nav.path = "/voice/settings";
});

it("test_shell_has_no_org_gate: a signed-in person with no organization gets the shell, and it provisions on open", () => {
  nav.path = "/voice"; nav.replace.mockClear();
  render(<VoiceShell><p>home</p></VoiceShell>);
  expect(screen.getByText("home")).toBeInTheDocument();
  expect(screen.queryByText("Choose your business")).not.toBeInTheDocument();
  expect(agent.provision).toHaveBeenCalled();
  expect(nav.replace).not.toHaveBeenCalled();
  nav.path = "/voice/settings";
});

it.each(["/voice", "/voice/settings", "/voice/agent"])("with no business saved, %s sends the person to name it", (path) => {
  agent.config = { business: "" }; nav.path = path; nav.replace.mockClear();
  render(<VoiceShell><p>page</p></VoiceShell>);
  expect(nav.replace.mock.calls).toEqual([["/onboarding"]]);
  expect(screen.queryByText("page")).not.toBeInTheDocument();
  agent.config = { business: "Acme" }; nav.path = "/voice/settings";
});

it("a live assistant is never sent back to name its business", () => {
  agent.config = { live: true }; nav.path = "/voice"; nav.replace.mockClear();
  render(<VoiceShell><p>page</p></VoiceShell>);
  expect(nav.replace).not.toHaveBeenCalled();
  agent.config = { business: "Acme" }; nav.path = "/voice/settings";
});

it("the rail is Lightbulb (the logo) · Home · Phone · Meetings · Needs you · Settings, and Agent and Workspace are gone", () => {
  render(<VoiceShell><div /></VoiceShell>);
  const links = screen.getAllByRole("link");
  expect(links.map((l) => l.getAttribute("href"))).toEqual(["/voice/lightbulb", "/voice", "/voice/agent", "/voice/meetings", "/voice/needs-you", "/voice/settings"]);
  expect(links.slice(1).map((l) => l.textContent)).toEqual(["Home", "Phone", "Meetings", "Needs you", "Settings"]);
  expect(screen.queryByRole("link", { name: "Agent" })).not.toBeInTheDocument();
  expect(links[0]).toHaveAccessibleName("Lightbulb");
  expect(links[0].querySelector("svg.brand-mark")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Workspace" })).not.toBeInTheDocument();
});

it("Needs you keeps its badge and count", () => {
  questions.list = [{ id: "q1" }, { id: "q2" }];
  render(<VoiceShell><div /></VoiceShell>);
  const needs = screen.getByRole("link", { name: "Needs you, 2" });
  expect(needs).toHaveAttribute("href", "/voice/needs-you");
  expect(needs.querySelector(".vx-badge")).toHaveTextContent("2");
  questions.list = [];
});


it("a user change creates a fresh talk and huddle owner", () => {
  const { rerender } = render(<VoiceShell><h1>Settings</h1></VoiceShell>);
  const first = screen.getByTestId("talk-owner");
  rerender(<VoiceShell><h1>Agent</h1></VoiceShell>);
  expect(screen.getByTestId("talk-owner")).toBe(first);
  org.userId = "another-user";
  rerender(<VoiceShell><h1>Agent</h1></VoiceShell>);
  expect(screen.getByTestId("talk-owner")).not.toBe(first);
  org.userId = "u";
});

it.each([["/voice/agent", true], ["/voice/meetings", true], ["/voice/lightbulb", true], ["/voice/settings", false], ["/voice", false]])(
  "with setup unfinished, %s sends the owner to Home: %s", (path, sent) => {
    agent.setup = true; nav.path = path; nav.replace.mockClear();
    render(<VoiceShell><p>page</p></VoiceShell>);
    expect(nav.replace.mock.calls).toEqual(sent ? [["/voice"]] : []);
    expect(screen.queryByText("page") === null).toBe(sent);
    agent.setup = false; nav.path = "/voice/settings";
  });

it("reads Stripe's return flag and says what happened, a tick after mount so the Toaster has subscribed", async () => {
  window.history.replaceState(null, "", "/voice/agent?voice=checkout_complete");
  render(<VoiceShell><div /></VoiceShell>);
  expect(toast.notifySuccess).not.toHaveBeenCalled();
  await waitFor(() => expect(toast.notifySuccess).toHaveBeenCalledWith("Payment received. Your assistant is going live.", expect.anything()));
  window.history.replaceState(null, "", "/voice/settings?voice=checkout_canceled");
  render(<VoiceShell><div /></VoiceShell>);
  await waitFor(() => expect(toast.notify).toHaveBeenCalledWith("Checkout cancelled. You have not been charged.", expect.anything()));
  window.history.replaceState(null, "", "/");
});

it("test_setup_forced_for_owner: with setup unfinished, the signed-in person is held on Home whatever their organization role", () => {
  agent.setup = true; nav.path = "/voice/needs-you"; nav.replace.mockClear();
  render(<VoiceShell>owner-page</VoiceShell>);
  expect(nav.replace).toHaveBeenCalledWith("/voice");
  expect(screen.queryByText("owner-page")).not.toBeInTheDocument();
  agent.setup = false; nav.path = "/voice/settings";
});
