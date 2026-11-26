// @vitest-environment jsdom
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BANNED } from "@/content/__tests__/bannedCopy";

import OsPage from "../page";

const s = vi.hoisted(() => ({ query: "", signedIn: false }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(s.query),
}));
const capture = vi.hoisted(() => vi.fn());
vi.mock("@/lib/analytics", () => ({ capture }));
vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));
vi.mock("next/font/google", () => ({ Bricolage_Grotesque: () => ({ variable: "" }), Silkscreen: () => ({ variable: "" }) }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: s.signedIn }),
  SignUp: () => <div data-testid="clerk-sign-up" />,
}));
const api = vi.hoisted(() => ({ post: vi.fn(async () => ({})), get: vi.fn() }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));

const DOWNLOAD = "Download for Mac";
const HERO_PRICE = "Free for 3 days, then $20/month. Renews monthly until you cancel.";

const stub = (status: number, config: unknown = { priceText: "$20/month", trialDays: 3 }) =>
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (!url.endsWith("/os/config")) return { status };
      if (config === null) throw new Error("down");
      return { ok: true, json: async () => config };
    }),
  );

async function page() {
  return render(await OsPage());
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubEnv("API_ORIGIN", "https://api-dev.thelightbulb.company");
  // jsdom has no canvas: the art renders its box and canvas and draws nothing.
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  stub(302);
  s.query = "";
  s.signedIn = false;
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("/os landing page (dev only)", () => {
  it("has the hero, eight scenes and six questions", async () => {
    const { container } = await page();
    expect(screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual([
      "Talk to your Mac. Spark does the clicking.",
    ]);
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual([
      "One sentence instead of all the clicking.",
      "Hold fn, say it, and Spark does it.",
      "It works in the apps you already use.",
      "Spark thinks with a plan you already have.",
      "Where your things go.",
      "Questions.",
      "Try Spark free for 3 days.",
    ]);
    expect(container.querySelectorAll("details")).toHaveLength(6);
    expect(container.textContent).toContain("Each friend who joins with your link adds a free day.");
    expect(container.textContent).toContain("For Mac with Apple silicon (M1 or later). Requires macOS 14 or later.");
  });

  it("the privacy lines stay as they are true today", async () => {
    const { container } = await page();
    const priv = [...container.querySelectorAll(".priv li")].map((li) => li.textContent);
    expect(priv).toEqual([
      "Your voice goes to OpenAI so Spark can understand and answer you. We don't store the audio.",
      "What Spark sees and does on your Mac goes to your own ChatGPT or Claude account, not to us.",
      "Your files and history stay on your Mac.",
    ]);
    const voice = [...container.querySelectorAll("details")].find((d) => d.textContent?.startsWith("Is my voice stored?"));
    expect(voice?.querySelector("p")?.textContent).toBe(
      "No. Your voice is processed by OpenAI to understand and answer you, and we do not store the audio. We keep a text transcript for 90 days to improve Spark.",
    );
  });

  it("the demo windows are pictures: hidden from screen readers, nothing in them to tab to", async () => {
    const { container } = await page();
    for (const sel of [".chat", ".daw", ".dia", ".sp-fixed"]) expect(container.querySelector(sel)).toHaveAttribute("aria-hidden", "true");
    container.querySelectorAll('[aria-hidden="true"]').forEach((el) => expect(el.querySelector("a, button, input")).toBeNull());
    container.querySelectorAll("canvas").forEach((c) => expect(c.closest('[aria-hidden="true"]')).not.toBeNull());
  });

  it("reads the price from the config", async () => {
    const { container } = await page();
    expect(screen.getByText(HERO_PRICE)).toBeInTheDocument();
    expect(container.textContent).toContain("Then $20/month.");
    expect(container.textContent).toContain("For 3 days. Then it's $20/month.");
  });

  it("drops every number when the config fails", async () => {
    stub(302, null);
    const { container } = await page();
    // The transcript retention (90 days) is a fixed fact, not a price: only prices and trial days must drop.
    const text = (container.textContent ?? "").replace("We keep a text transcript for 90 days", "");
    expect(text).not.toMatch(/\$|\d+ days/);
    expect(screen.getByRole("heading", { name: "Try Spark." })).toBeInTheDocument();
    expect(text).toContain("See the price when you download.");
    expect(screen.queryByText(HERO_PRICE)).toBeNull();
  });

  it("both Download buttons count their placement", async () => {
    await page();
    const links = screen.getAllByRole("link", { name: DOWNLOAD });
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["/download/spark", "/download/spark"]);
    links.forEach((a) => fireEvent.click(a));
    expect(capture.mock.calls).toEqual([
      ["spark_download_clicked", { source: "os_hero" }],
      ["spark_download_clicked", { source: "os_closing" }],
    ]);
  });

  it("installer not published: coming soon in both places", async () => {
    stub(404);
    await page();
    expect(screen.queryByRole("link", { name: DOWNLOAD })).toBeNull();
    expect(screen.getAllByText("Download coming soon.")).toHaveLength(2);
  });

  it("an invite puts the sign-up in the hero and keeps the sections", async () => {
    s.query = "ref=abc123XYZ";
    const { container } = await page();
    const hero = container.querySelector(".sp-hero") as HTMLElement;
    expect(within(hero).getByTestId("clerk-sign-up")).toBeInTheDocument();
    expect(
      within(hero).getByText("You've been invited to Spark. Create your account, then download."),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Hold fn, say it, and Spark does it." })).toBeInTheDocument();
    expect(container.querySelectorAll("details")).toHaveLength(6);
  });

  it("no dashes, banned words, AI headline or price outside the config lines", async () => {
    const { container } = await page();
    const bold = [...container.querySelectorAll("h1, h2, h3, b")].map((el) => el.textContent ?? "");
    expect(bold.filter((t) => /\bAI\b/.test(t))).toEqual([]);
    const text = (container.textContent ?? "").replaceAll("$20/month", "");
    expect(text).not.toMatch(/[—–]/);
    expect(text).not.toMatch(BANNED);
    expect(text).not.toMatch(/\$/);
  });


});
