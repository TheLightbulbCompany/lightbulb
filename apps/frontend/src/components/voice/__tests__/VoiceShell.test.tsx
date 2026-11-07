import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/voice/settings" }));
vi.mock("@clerk/nextjs", () => ({ UserButton: () => null, useOrganization: () => ({ organization: { id: "o" }, isLoaded: true }) }));
vi.mock("@/components/huddle/HuddleProvider", () => ({ HuddleProvider: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("../data", () => ({ callNeedsYou: () => false, useQuestions: () => ({ data: [] }), useTimeline: () => ({ data: [] }) }));

import { VoiceShell } from "../VoiceShell";

it("Settings rail item opens /voice/settings", () => {
  render(<VoiceShell>x</VoiceShell>);
  const settings = screen.getByRole("link", { name: "Settings" });
  expect(settings).toHaveAttribute("href", "/voice/settings");
  expect(settings).toHaveAttribute("aria-current", "page");
});
