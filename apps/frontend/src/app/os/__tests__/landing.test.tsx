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
  vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api-dev.thelightbulb.company/api/v1");
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
  it("has the hero, every section and six questions", async () => {
    const { container } = await page();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Meet Spark");
    expect(container.textContent).toContain("One-click voice interface for your Mac.");
    expect(container.querySelector(".os-how-line")?.textContent).toBe(
      "Hold fn and say what you want done. Spark does it on your Mac.",
    );
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual([
      "How Spark works",
      "What Spark can do",
      "Runs on your ChatGPT or Claude",
      "Where your things go",
      "Questions",
      "Try Spark free for 3 days.",
    ]);
    expect(container.querySelectorAll("details")).toHaveLength(6);
    expect(container.textContent).toContain("In Spark, open Settings › Account › Manage subscription.");
    expect(container.textContent).toContain(
      "Spark runs on your own ChatGPT account or a Claude plan of Pro or higher. Claude Free cannot run it.",
    );
    expect(container.textContent).toContain("Each friend who joins with your link adds a free day.");
  });

  it("reads the price from the config", async () => {
    const { container } = await page();
    expect(screen.getByText(HERO_PRICE)).toBeInTheDocument();
    expect(container.textContent).toContain("Then $20/month.");
    expect(container.textContent).toContain("Free for 3 days, then $20/month. It renews monthly until you cancel.");
  });

  it("drops every number when the config fails", async () => {
    stub(302, null);
    const { container } = await page();
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/\$|\d+ days/);
    expect(screen.getByRole("heading", { name: "Try Spark." })).toBeInTheDocument();
    expect(text).toContain("See the price when you download.");
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
    const hero = container.querySelector(".os-hero") as HTMLElement;
    expect(within(hero).getByTestId("clerk-sign-up")).toBeInTheDocument();
    expect(
      within(hero).getByText("You've been invited to Spark. Create your account, then download."),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "How Spark works" })).toBeInTheDocument();
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

  it("the art is a canvas hidden from screen readers, the hero described once", async () => {
    const { container } = await page();
    const canvases = container.querySelectorAll("canvas");
    expect(canvases).toHaveLength(6); // hero, four setup bulbs, closing
    canvases.forEach((c) => expect(c).toHaveAttribute("aria-hidden", "true"));
    expect(screen.getByRole("img").getAttribute("aria-label")).toMatch(/^Spark: a small blue creature/);
  });

  it("describes today's first conversation, not the old tidy-your-Desktop lesson", async () => {
    const { container } = await page();
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/tid(y|ies) your Desktop/i);
    expect(text).toContain("Its first hello: it circles what it sees, then does one small thing you ask.");
    expect(text).toContain("Say hi. Spark shows what it sees and does one small thing.");
  });

  it("shares Spark's own 1200 x 630 picture", async () => {
    const { metadata } = await import("../page");
    expect(metadata.openGraph?.images).toEqual([
      { url: "/og-spark.jpg", width: 1200, height: 630, alt: "Meet Spark. One-click voice interface for your Mac." },
    ]);
    expect(metadata.twitter?.images).toEqual(["/og-spark.jpg"]);
  });
});
