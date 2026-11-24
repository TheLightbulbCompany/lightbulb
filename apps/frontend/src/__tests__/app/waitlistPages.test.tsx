import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));
vi.mock("@/components/landing/PricingThreeCard", () => ({ PricingThreeCard: () => null }));
const pricing = vi.hoisted(() => ({ value: null as null | { voiceMonthlyUsd: number; voiceIncludedMinutes: number } }));
vi.mock("@/lib/pricing", async (orig) => ({
  ...(await orig<typeof import("@/lib/pricing")>()),
  getPricing: async () => pricing.value,
  voicePlanLine: (p: unknown) => (p ? "PLAN LINE FROM THE BACKEND." : null),
}));
// The Spark (/os), Phone and Meetings pages load their own fonts; jsdom has no next/font.
vi.mock("next/font/google", () => {
  const font = () => ({ variable: "" });
  return { Bricolage_Grotesque: font, Silkscreen: font, Schibsted_Grotesk: font, Kalam: font, Atkinson_Hyperlegible: font };
});
const captureSignupCta = vi.fn();
vi.mock("@/lib/signupFlow", () => ({ captureSignupCta: (o: unknown) => captureSignupCta(o) }));

import HubPage, { metadata as hubMeta } from "@/app/page";
import PhonePage, { metadata as phoneMeta } from "@/app/phone/page";
import MeetingsPage, { metadata as meetingsMeta } from "@/app/meetings/page";
import OsPage, { metadata as osMeta } from "@/app/os/page";
import { BANNED } from "@/content/__tests__/bannedCopy";
import { ADDRESS } from "@/components/landing/MeetingsLanding";
import { HOME_FAQ } from "@/lib/homeFaq";

const pages = [
  { path: "/meetings", Page: MeetingsPage, meta: meetingsMeta, h1: "Ask your meeting anything. Out loud." },
];

describe.each(pages)("$path", ({ path, Page, meta, h1 }) => {
  it("renders its headline and a waitlist form", () => {
    const { container } = render(<Page />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(h1);
    expect(container.querySelectorAll("h1")).toHaveLength(1);
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
    // The invite address is the product's real one (agents@…), not copy.
    const text = (container.textContent ?? "").replaceAll(ADDRESS, "");
    expect(text).not.toMatch(BANNED);
    expect(text).not.toMatch(/\$\d/);
    expect(text).not.toMatch(/[—–]/);
  });
});

describe("/meetings, the call", () => {
  it("keeps the form under the menu's #waitlist anchor and says Nova is an example, on Google Meet only", () => {
    const { container } = render(<MeetingsPage />);
    expect(container.querySelector("#waitlist form")).not.toBeNull();
    const text = container.textContent ?? "";
    expect(text).toContain("Here it's called Nova. You pick the name.");
    expect(text).toContain("Google Meet only. Not Zoom, Microsoft Teams or Webex.");
    expect([...container.querySelectorAll('main a[href="#waitlist"]')]).toHaveLength(2);
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

const HOME_H1 = "Voice to run your life, business and computer";

describe("/", () => {
  it("says the whole line once, and lets the word turn only for the eye", () => {
    const { container } = render(<HubPage />);
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1, name: HOME_H1 })).toBeTruthy();
    expect(container.querySelector("#word")?.closest("[aria-hidden=true]")).toBeTruthy();
    expect(container.textContent).toContain("Say it like you'd say it to a person.");
    expect(container.textContent).toContain("Examples, not real customers");
  });

  it("opens the window on Spark, then Phone and Meetings, and the arrow keys move between them", () => {
    render(<HubPage />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Spark", "Phone", "Meetings"]);
    expect(tabs.map((t) => t.getAttribute("aria-selected"))).toEqual(["true", "false", "false"]);
    expect(screen.getByRole("tabpanel").id).toBe("p-spark");
    fireEvent.keyDown(tabs[0], { key: "ArrowRight" });
    expect(tabs[1].getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel").id).toBe("p-phone");
    expect(document.activeElement).toBe(tabs[1]);
    fireEvent.keyDown(tabs[1], { key: "End" });
    expect(screen.getByRole("tabpanel").id).toBe("p-meetings");
    fireEvent.keyDown(tabs[2], { key: "ArrowRight" });
    expect(screen.getByRole("tabpanel").id).toBe("p-spark");
  });

  it("gives each product a row and its own door, Spark first", () => {
    const { container } = render(<HubPage />);
    expect([...container.querySelectorAll(".prow h2")].map((h) => h.textContent)).toEqual([
      "Spark",
      "Lightbulb Phone",
      "Lightbulb Meetings",
    ]);
    expect(screen.getByRole("link", { name: "Download for Mac" }).getAttribute("href")).toBe("/download/spark");
    expect(screen.getByRole("link", { name: "Set up Lightbulb Phone" }).getAttribute("href")).toBe("/sign-up");
    expect(screen.getByRole("link", { name: "Join the waitlist" }).getAttribute("href")).toBe("/meetings#waitlist");
    expect(screen.getByRole("link", { name: "Talk to a founder" }).getAttribute("href")).toBe("/request-setup");
    for (const img of container.querySelectorAll(".prow img")) expect(img.getAttribute("alt")).toMatch(/page/);
  });

  it("keeps AI out of every bold headline", () => {
    const { container } = render(<HubPage />);
    const bold = [...container.querySelectorAll("h1, h2, h3, b")].map((el) => el.textContent ?? "");
    expect(bold.filter((t) => /\bAI\b/.test(t))).toEqual([]);
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
    fireEvent.click(screen.getByRole("link", { name: "Set up Lightbulb Phone" }));
    fireEvent.click(screen.getByRole("link", { name: "Talk to a founder" }));
    expect(captureSignupCta.mock.calls.map(([o]) => o.ctaId)).toEqual(["hub_sign_up", "hub_founder"]);
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
