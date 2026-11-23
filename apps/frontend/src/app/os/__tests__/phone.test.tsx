// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import OsPage from "../page";
import { DownloadBlock } from "../OsInvite";
import { ON_PHONE, isPhoneAgent } from "../SparkDownload";

const s = vi.hoisted(() => ({ query: "", signedIn: false }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(s.query) }));
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

const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1";
const MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15";
const CODE = "0TestCodeNotARealPerson0000";

const ua = (value: string) => vi.spyOn(navigator, "userAgent", "get").mockReturnValue(value);

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url.endsWith("/os/config") ? { ok: true, json: async () => ({ priceText: "$20/month", trialDays: 3 }) } : { status: 302 },
    ),
  );
  s.query = "";
  s.signedIn = false;
  window.history.replaceState(null, "", "/os");
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("isPhoneAgent", () => {
  it("phones and tablets, including an iPad that calls itself a Mac", () => {
    expect(isPhoneAgent(IPHONE, 5)).toBe(true);
    expect(isPhoneAgent("Mozilla/5.0 (Linux; Android 14; Pixel 8)", 5)).toBe(true);
    expect(isPhoneAgent(MAC, 5)).toBe(true);
    expect(isPhoneAgent(MAC, 0)).toBe(false);
  });
});

describe("/os on a phone", () => {
  it("replaces both download buttons with the Mac line, a copy and an email of the link", async () => {
    ua(IPHONE);
    const writeText = vi.fn(async () => undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(await OsPage());
    expect(screen.queryByRole("link", { name: "Download for Mac" })).toBeNull();
    expect(screen.getAllByText(ON_PHONE)).toHaveLength(2);
    const mail = screen.getAllByRole("link", { name: "Email me the link" })[0];
    expect(mail.getAttribute("href")).toBe(
      `mailto:?subject=Spark%20for%20Mac&body=${encodeURIComponent(`${window.location.origin}/os`)}`,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "Copy link" })[0]);
    await waitFor(() => expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument());
    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/os`);
  });

  it("an invite keeps its code in the link, and the sign-up form stays without a download", async () => {
    ua(IPHONE);
    s.query = `ref=${CODE}`;
    window.history.replaceState(null, "", `/os?ref=${CODE}`);
    render(await OsPage());
    expect(screen.getByTestId("clerk-sign-up")).toBeInTheDocument();
    expect(screen.queryByText("Skip, just download")).toBeNull();
    const mail = screen.getAllByRole("link", { name: "Email me the link" })[0];
    expect(decodeURIComponent(mail.getAttribute("href") ?? "")).toContain(`/os?ref=${CODE}`);
  });

  it("after signing up on the phone: the Mac line, not a download", async () => {
    ua(IPHONE);
    s.query = `ref=${CODE}`;
    s.signedIn = true;
    render(await OsPage());
    await waitFor(() => expect(screen.getAllByText(ON_PHONE)).toHaveLength(2));
    expect(screen.queryByRole("link", { name: "Download for Mac" })).toBeNull();
  });

  it("a Mac still gets the downloads", async () => {
    ua(MAC);
    render(await OsPage());
    expect(screen.getAllByRole("link", { name: "Download for Mac" })).toHaveLength(2);
    expect(screen.queryByText(ON_PHONE)).toBeNull();
  });

  it("the server HTML always carries the download (crawlers; the hero's Suspense fallback)", () => {
    ua(IPHONE);
    const html = renderToString(<DownloadBlock ready source="os_hero" />);
    expect(html).toContain('href="/download/spark"');
    expect(html).not.toContain(ON_PHONE);
  });
});
