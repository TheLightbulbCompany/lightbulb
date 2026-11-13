import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/voice/settings" }));
const org = vi.hoisted(() => ({ current: { id: "o" } as { id: string } | null, userId: "u", list: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({
  UserButton: () => null,
  useAuth: () => ({ userId: org.userId }),
  OrganizationList: (p: object) => { org.list(p); return <p>org-list</p>; },
  useOrganization: () => ({ organization: org.current, isLoaded: true }),
}));
vi.mock("../TalkSessionProvider", () => ({ TalkSessionProvider: ({ children }: { children: React.ReactNode }) => <div data-testid="talk-owner">{children}</div> }));
vi.mock("@/components/huddle/HuddleProvider", () => ({ HuddleProvider: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
const questions = vi.hoisted(() => ({ list: [] as { id: string }[] }));
vi.mock("../data", () => ({ LIGHTBULB: "Lightbulb", useQuestions: () => ({ data: questions.list }), useGoLiveAfterPayment: () => {} }));

import { VoiceShell } from "../VoiceShell";

it("Settings rail item opens /voice/settings", () => {
  render(<VoiceShell>x</VoiceShell>);
  const settings = screen.getByRole("link", { name: "Settings" });
  expect(settings).toHaveAttribute("href", "/voice/settings");
  expect(settings).toHaveAttribute("aria-current", "page");
});

it("no-org user at /voice sees OrganizationList with afterSelect /voice", () => {
  org.current = null;
  render(<VoiceShell>x</VoiceShell>);
  expect(screen.getByText("org-list")).toBeInTheDocument();
  expect(org.list).toHaveBeenCalledWith(expect.objectContaining({ afterSelectOrganizationUrl: "/voice", afterCreateOrganizationUrl: "/onboarding" }));
  org.current = { id: "o" };
});

it("the rail is Lightbulb (the logo) · Home · Agent · Needs you · Settings, and Workspace is gone", () => {
  render(<VoiceShell><div /></VoiceShell>);
  const links = screen.getAllByRole("link");
  expect(links.map((l) => l.getAttribute("href"))).toEqual(["/voice/lightbulb", "/voice", "/voice/agent", "/voice/needs-you", "/voice/settings"]);
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


it("a workspace or user change creates a fresh talk and huddle owner", () => {
  const { rerender } = render(<VoiceShell><h1>Settings</h1></VoiceShell>);
  const first = screen.getByTestId("talk-owner");
  rerender(<VoiceShell><h1>Agent</h1></VoiceShell>);
  expect(screen.getByTestId("talk-owner")).toBe(first);
  org.current = { id: "another-workspace" };
  rerender(<VoiceShell><h1>Agent</h1></VoiceShell>);
  const second = screen.getByTestId("talk-owner");
  expect(second).not.toBe(first);
  org.userId = "another-user";
  rerender(<VoiceShell><h1>Agent</h1></VoiceShell>);
  expect(screen.getByTestId("talk-owner")).not.toBe(second);
  org.current = { id: "o" }; org.userId = "u";
});
