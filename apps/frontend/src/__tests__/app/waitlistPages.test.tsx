import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));
vi.mock("@/components/landing/PricingThreeCard", () => ({ PricingThreeCard: () => null }));
vi.mock("@/lib/pricing", () => ({ getPricing: async () => null }));
const captureSignupCta = vi.fn();
vi.mock("@/lib/signupFlow", () => ({ captureSignupCta: (o: unknown) => captureSignupCta(o) }));

import HubPage, { metadata as hubMeta } from "@/app/page";
import PhonePage, { metadata as phoneMeta } from "@/app/phone/page";
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

// The founder's own wording (20 Nov): the one bold headline allowed to say "AI".
const HUB_H1_AI_EXCEPTION = "The number one voice AI application company.";

describe("/", () => {
  it("leads with the company line and links to all three products", () => {
    const { container } = render(<HubPage />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(HUB_H1_AI_EXCEPTION);
    const hrefs = [...container.querySelectorAll("main a")].map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(expect.arrayContaining(["/phone", "/meetings", "/os", "/sign-up", "/request-setup"]));
    expect(screen.getByRole("link", { name: "See Lightbulb Phone" }).getAttribute("href")).toBe("/phone");
    expect(screen.getByRole("link", { name: "Join the Meetings waitlist" }).getAttribute("href")).toBe("/meetings");
    expect(screen.getByRole("link", { name: "Join the OS waitlist" }).getAttribute("href")).toBe("/os");
  });

  it("keeps AI out of every other bold headline", () => {
    const { container } = render(<HubPage />);
    const bold = [...container.querySelectorAll("h1, h2, b")].map((el) => el.textContent ?? "");
    expect(bold.filter((t) => t !== HUB_H1_AI_EXCEPTION && /\bAI\b/.test(t))).toEqual([]);
  });

  it("carries no FAQ, pricing, banned word, price or dash", () => {
    const { container } = render(<HubPage />);
    const text = container.textContent ?? "";
    expect(container.querySelector("#faq, #voices, #pricing")).toBeNull();
    expect(text).not.toMatch(BANNED);
    expect(text).not.toMatch(/\$\d/);
    expect(text).not.toMatch(/[—–]/);
  });

  it("tracks the sign-up door and is the canonical company home", () => {
    captureSignupCta.mockClear();
    render(<HubPage />);
    fireEvent.click(screen.getByRole("link", { name: /Set up Lightbulb Phone/ }));
    expect(captureSignupCta.mock.calls.map(([o]) => o.ctaId)).toEqual(["hub_sign_up"]);
    expect(hubMeta.alternates?.canonical).toBe("https://thelightbulb.company/");
    expect(String(hubMeta.description)).not.toMatch(/salon|receptionist/i);
  });
});

describe("/phone", () => {
  it("is today's home under its own canonical", async () => {
    render(await PhonePage());
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
      "Your phone, answered and booked while your hands are busy.",
    );
    expect(phoneMeta.alternates?.canonical).toBe("https://thelightbulb.company/phone");
  });
});
