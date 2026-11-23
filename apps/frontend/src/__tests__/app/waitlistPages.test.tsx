import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));
const pricing = vi.hoisted(() => ({ value: null as null | { voiceMonthlyUsd: number; voiceIncludedMinutes: number } }));
vi.mock("@/lib/pricing", async (orig) => ({
  ...(await orig<typeof import("@/lib/pricing")>()),
  getPricing: async () => pricing.value,
  voicePlanLine: (p: unknown) => (p ? "PLAN LINE FROM THE BACKEND." : null),
}));
// The Spark (/os) and Phone pages load their own fonts; jsdom has no next/font.
vi.mock("next/font/google", () => {
  const font = () => ({ variable: "" });
  return { Bricolage_Grotesque: font, Silkscreen: font, Schibsted_Grotesk: font, Kalam: font };
});
const captureSignupCta = vi.fn();
vi.mock("@/lib/signupFlow", () => ({ captureSignupCta: (o: unknown) => captureSignupCta(o) }));

import HubPage, { metadata as hubMeta } from "@/app/page";
import PhonePage, { metadata as phoneMeta } from "@/app/phone/page";
import MeetingsPage, { metadata as meetingsMeta } from "@/app/meetings/page";
import OsPage, { metadata as osMeta } from "@/app/os/page";
import { BANNED } from "@/content/__tests__/bannedCopy";
import { HOME_FAQ } from "@/lib/homeFaq";

const pages = [
  { path: "/meetings", Page: MeetingsPage, meta: meetingsMeta, h1: "Leave every meeting with the notes already written." },
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

  it("keeps AI out of every bold headline", () => {
    const { container } = render(<Page />);
    const bold = [...container.querySelectorAll("h1, h2, h3, b")].map((el) => el.textContent ?? "");
    expect(bold.filter((t) => /\bAI\b/.test(t))).toEqual([]);
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
  // The page is async: it asks GitHub whether the installer is published (a 302 when it is) and the
  // backend for Spark's price (GET /os/config).
  const LINE = "Free for 3 days, then $20/month. Renews monthly until you cancel.";
  // Production's short page; the dev-only landing page is tested in app/os/__tests__/landing.test.tsx.
  beforeEach(() => vi.stubEnv("SPARK_SHORT_PAGE", "1"));
  beforeEach(() =>
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.endsWith("/os/config")
          ? { ok: true, json: async () => ({ priceText: "$20/month", trialDays: 3 }) }
          : { status: 302 },
      ),
    ),
  );
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("meets Spark and offers the Mac download, with no waitlist form", async () => {
    const { container } = render(await OsPage());
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Meet Spark");
    expect(container.textContent).toContain("One-click voice interface for your Mac.");
    expect(container.textContent).toContain("For Mac with Apple silicon (M1 or later). Requires macOS 14 or later.");
    expect(screen.getByRole("link", { name: "Download for Mac" }).getAttribute("href")).toBe("/download/spark");
    expect(container.querySelector("form")).toBeNull();
    expect(osMeta.alternates?.canonical).toBe("https://thelightbulb.company/os");
    expect(String(osMeta.title)).toContain("Spark");
  });

  it("keeps AI, banned words and dashes out, and a price only in the one line from the config", async () => {
    const { container } = render(await OsPage());
    const text = container.textContent ?? "";
    expect(container.querySelector("h1")?.textContent).not.toMatch(/\bAI\b/);
    expect(text.split(LINE)).toHaveLength(2);
    expect(text.replace(LINE, "")).not.toMatch(BANNED); // BANNED includes any price
    expect(text).not.toMatch(/[—–]/);
  });
});

// The founder's own wording (20 Nov): the one bold headline allowed to say "AI".
const HUB_H1_AI_EXCEPTION = "The voice AI application company.";

describe("/", () => {
  it("leads with the company line and gives each product its own card and door", () => {
    const { container } = render(<HubPage />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(HUB_H1_AI_EXCEPTION);
    const cards = container.querySelectorAll(".lb-hub-card");
    expect([...cards].map((c) => c.querySelector("h2")?.textContent)).toEqual([
      "Lightbulb Phone",
      "Lightbulb Meetings",
      "Spark",
    ]);
    const spark = cards[2];
    expect(spark.querySelector("p")?.textContent).toBe("One-click voice interface for your Mac.");
    expect(screen.getByRole("link", { name: "Download for Mac" }).getAttribute("href")).toBe("/os");
    expect(screen.getByRole("link", { name: "Set up Lightbulb Phone" }).getAttribute("href")).toBe("/sign-up");
    expect(screen.getAllByRole("link", { name: "Join the waitlist" }).map((a) => a.getAttribute("href"))).toEqual([
      "/meetings",
    ]);
    expect(screen.getByRole("link", { name: "See how it works" }).getAttribute("href")).toBe("/phone");
    expect(screen.getByRole("link", { name: "Talk to a founder" }).getAttribute("href")).toBe("/request-setup");
    // The salon mock-up lives on /phone only.
    expect(container.querySelector(".lb-art")).toBeNull();
  });

  it("keeps AI out of every other bold headline", () => {
    const { container } = render(<HubPage />);
    const bold = [...container.querySelectorAll("h1, h2, h3, b")].map((el) => el.textContent ?? "");
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
    expect(hubMeta.title).toBe("The Voice AI Application Company | Lightbulb");
    expect(String(hubMeta.description)).not.toMatch(/salon|receptionist/i);
  });
});

describe("/phone", () => {
  afterEach(() => {
    pricing.value = null;
  });

  it("is today's home under its own canonical, with one h1", async () => {
    const { container } = render(await PhonePage());
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
      "Your phone, answered and booked while your hands are busy.",
    );
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    expect(phoneMeta.alternates?.canonical).toBe("https://thelightbulb.company/phone");
  });

  it("tracks both sign-up doors and points Hear the voices at the stock samples", async () => {
    captureSignupCta.mockClear();
    const { container } = render(await PhonePage());
    const doors = screen.getAllByRole("link", { name: "Set up your assistant" });
    expect(doors.map((a) => a.getAttribute("href"))).toEqual(["/sign-up", "/sign-up"]);
    doors.forEach((a) => fireEvent.click(a));
    expect(captureSignupCta.mock.calls.map(([o]) => o.ctaId)).toEqual(["hero_start_free", "closing_start_free"]);
    expect(screen.getAllByRole("link", { name: "Hear the voices" }).map((a) => a.getAttribute("href"))).toEqual([
      "#voices",
      "#voices",
    ]);
    const voices = container.querySelector("#voices")!;
    expect(voices.textContent).toContain("These are stock samples.");
    expect([...voices.querySelectorAll(".vc a")].map((a) => [a.getAttribute("aria-label"), a.getAttribute("href")])).toEqual([
      ["Play Marin, a stock sample", "/voice-samples/gpt-live-1-marin-en.wav"],
      ["Play Quartz, a stock sample", "/voice-samples/gpt-live-1-quartz-en.wav"],
      ["Play Willow, a stock sample", "/voice-samples/gpt-live-1-willow-en.wav"],
    ]);
  });

  it("says Nova is an example name and shows the visible questions the structured data carries", async () => {
    const { container } = render(await PhonePage());
    expect(container.textContent).toContain("Name it Nova, or anything you like.");
    const faq = [...container.querySelectorAll("#faq details")].map((d) => [
      d.querySelector("summary")?.textContent,
      d.querySelector("p")?.textContent,
    ]);
    expect(faq).toEqual(HOME_FAQ.map(({ q, a }) => [q, a]));
  });

  it("reads the price from the backend and never invents one", async () => {
    const neutral = render(await PhonePage());
    expect(neutral.container.textContent).toContain("One monthly plan with minutes included. Month to month");
    expect(neutral.container.textContent).not.toMatch(/\$\d/);
    expect(neutral.container.textContent).not.toMatch(/[—–]/);
    neutral.unmount();
    pricing.value = { voiceMonthlyUsd: 1, voiceIncludedMinutes: 1 };
    const priced = render(await PhonePage());
    expect(priced.container.textContent).toContain("PLAN LINE FROM THE BACKEND. Month to month");
  });
});
