// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/image", () => ({ default: () => null }));
vi.mock("next/navigation", () => ({ usePathname: () => "/" }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ isLoaded: true, isSignedIn: false }), UserButton: () => null }));
const captureSignupCta = vi.fn();
vi.mock("@/lib/signupFlow", () => ({ captureSignupCta: (o: unknown) => captureSignupCta(o) }));

import { HOME_FAQ } from "@/lib/homeFaq";

import { Footer } from "../Footer";
import { Hero } from "../Hero";
import { HomeFaq } from "../HomeFaq";
import { AfterTheCall, InCharge, OnTheCall, Voices } from "../HomeSections";
import { HowItWorks } from "../HowItWorks";
import { Navbar } from "../Navbar";
import { ProblemBand } from "../ProblemBand";

// Words the product context bans from customer copy (.agents/product-marketing.md, "Words to avoid").
const BANNED =
  /\bagents?\b|AI employees|workspace|unlimited|never miss|recordings?\b|\bSMS\b|\btexts?\b|reminders?|no-shows?|free trial|HIPAA|\bBAA\b|patients?|human-like|indistinguishable|replaces your staff|in minutes|15 minutes|revolutioni[sz]e|seamless|supercharge|Paperclip|Hermes|AgentCore/i;

function page() {
  return render(
    <>
      <Navbar />
      <Hero />
      <ProblemBand />
      <HowItWorks />
      <OnTheCall />
      <Voices />
      <AfterTheCall />
      <InCharge />
      <HomeFaq />
      <Footer />
    </>,
  );
}

describe("Phone page voice", () => {
  it("uses none of the banned words and types no price", () => {
    const { container } = page();
    const text = `${container.textContent} ${HOME_FAQ.map(({ q, a }) => `${q} ${a}`).join(" ")}`;
    expect(text).not.toMatch(BANNED);
    // Prices come from the billing API, never from copy.
    expect(text).not.toMatch(/\$\s?\d/);
    // The one figure on the page is third-party and cited next to it.
    expect(text).toContain("44% never reached a person. (Invoca, 2026 benchmarks)");
  });

  it("reads the headlines as written", () => {
    const { container } = page();
    const flat = (container.textContent ?? "").replace(/\s+/g, " ");
    expect(flat).toContain("Your phone, answered and booked while your hands are busy.");
    expect(flat).toContain("Set up in five steps. Here is what they come down to.");
    expect(flat).toContain("What your assistant does on a call");
    expect(flat).toContain("After every call, you see exactly what happened.");
    expect(flat).toContain("You stay in charge");
  });

  it("keeps AI out of every bold headline", () => {
    const { container } = page();
    const bold = [...container.querySelectorAll("h1, h2, h3, b")].map((el) => el.textContent ?? "");
    expect(bold.filter((t) => /\bAI\b/.test(t))).toEqual([]);
    expect(container.textContent).not.toContain("honest about being an AI");
    expect(container.textContent).toContain("It never pretends to be a person");
  });

  it("links only to pages that exist after the pivot", () => {
    const { container } = page();
    const allowed = new Set([
      "/", "/phone", "/meetings", "/os", "/pricing", "/compare", "/request-setup", "/security", "/support",
      "/sign-in", "/sign-up", "/network", "/privacy", "/terms", "/cookies", "/disclaimer", "/eula",
      "/acceptable-use", "/accessibility", "#voices",
      "https://invoca.com/reports/the-invoca-lead-conversion-benchmarks-report-2026",
    ]);
    const hrefs = [...container.querySelectorAll("a")].map((a) => a.getAttribute("href") ?? "");
    expect(hrefs.filter((h) => !allowed.has(h))).toEqual([]);
  });

  it("plays the three stock voices in English and Spanish", () => {
    const { container } = render(<Voices />);
    const srcs = [...container.querySelectorAll("audio")].map((a) => a.getAttribute("src"));
    expect(srcs).toHaveLength(6);
    expect(srcs).toContain("/voice-samples/gpt-live-1-marin-en.wav");
    expect(srcs).toContain("/voice-samples/gpt-live-1-willow-es.wav");
    expect(container.textContent).toContain("These are stock samples");
  });

  it("tracks both footer doors", () => {
    captureSignupCta.mockClear();
    render(<Footer />);
    fireEvent.click(screen.getByRole("link", { name: /Set up Lightbulb Phone/ }));
    fireEvent.click(screen.getAllByRole("link", { name: "Talk to a founder" })[0]);
    expect(captureSignupCta.mock.calls.map(([o]) => o.ctaId)).toEqual(["footer_start_free", "footer_setup"]);
  });

  it("names the three products and pricing in the nav, in order", () => {
    const { container } = render(<Navbar />);
    const links = [...container.querySelectorAll(".nav-links a:not(.nav-mobile-login)")].map((a) => a.textContent);
    expect(links).toEqual(["Phone", "Meetings", "OS", "Pricing"]);
    expect(container.textContent).toContain("Set up Lightbulb Phone");
    expect(container.textContent).not.toContain("Set up your assistant");
  });

  it("tracks the nav's sign-up and founder doors", () => {
    captureSignupCta.mockClear();
    render(<Navbar />);
    fireEvent.click(screen.getByRole("link", { name: "Talk to a founder" }));
    fireEvent.click(screen.getAllByRole("link", { name: /Set up/ }).at(-1)!);
    expect(captureSignupCta.mock.calls.map(([o]) => o.ctaId)).toEqual(["nav_request_setup", "nav_sign_up"]);
  });
});
