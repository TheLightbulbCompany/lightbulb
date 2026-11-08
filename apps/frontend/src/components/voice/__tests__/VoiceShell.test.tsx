import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/voice/settings" }));
const org = vi.hoisted(() => ({ current: { id: "o" } as { id: string } | null, list: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({
  UserButton: () => null,
  OrganizationList: (p: object) => { org.list(p); return <p>org-list</p>; },
  useOrganization: () => ({ organization: org.current, isLoaded: true }),
}));
vi.mock("@/components/huddle/HuddleProvider", () => ({ HuddleProvider: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("../data", () => ({ useQuestions: () => ({ data: [] }) }));

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

it("Workspace rail item opens the super-agent UI at /chat", () => {
  render(<VoiceShell><div /></VoiceShell>);
  expect(screen.getByRole("link", { name: "Workspace" })).toHaveAttribute("href", "/chat");
});
