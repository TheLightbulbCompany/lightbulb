import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));

import MeetingsPage, { metadata as meetingsMeta } from "@/app/meetings/page";
import OsPage, { metadata as osMeta } from "@/app/os/page";
import { BANNED } from "@/content/__tests__/bannedCopy";

const pages = [
  { path: "/meetings", Page: MeetingsPage, meta: meetingsMeta, h1: "Leave every meeting with the notes already written." },
  { path: "/os", Page: OsPage, meta: osMeta, h1: "Your Mac, by voice." },
];

describe.each(pages)("$path", ({ path, Page, meta, h1 }) => {
  it("renders its headline and a waitlist form", () => {
    const { container } = render(<Page />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(h1);
    const button = screen.getByRole("button", { name: "Join the waitlist" });
    expect(button.closest("form")).not.toBeNull();
    expect(container.querySelector('main a[href="/sign-up"]')).toBeNull();
  });

  it("has its own canonical and a description off the phone product", () => {
    expect(meta.alternates?.canonical).toBe(`https://thelightbulb.company${path}`);
    expect(String(meta.description)).not.toMatch(/salon|receptionist/i);
  });

  it("carries no banned word, price or dash", () => {
    const { container } = render(<Page />);
    const text = container.textContent ?? "";
    expect(text).not.toMatch(BANNED);
    expect(text).not.toMatch(/\$\d/);
    expect(text).not.toMatch(/[—–]/);
  });
});

describe("/os", () => {
  it("offers no download", () => {
    const { container } = render(<OsPage />);
    for (const a of container.querySelectorAll("a")) {
      expect(a.getAttribute("href") ?? "").not.toMatch(/download|\.dmg/i);
    }
  });
});
