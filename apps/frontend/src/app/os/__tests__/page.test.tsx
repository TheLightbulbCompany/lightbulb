// @vitest-environment jsdom
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import OsPage from "../page";

const s = vi.hoisted(() => ({ query: "", signedIn: false }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(s.query) }));
const capture = vi.hoisted(() => vi.fn());
vi.mock("@/lib/analytics", () => ({ capture }));
vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));
const signUpProps = vi.hoisted(() => vi.fn());
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: s.signedIn }),
  SignUp: (props: unknown) => {
    signUpProps(props);
    return <div data-testid="clerk-sign-up" />;
  },
}));
const post = vi.hoisted(() => vi.fn());
const api = vi.hoisted(() => ({ post, get: vi.fn() }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));

const DOWNLOAD = "Download for Mac";
const SAME = "Open Spark and sign in with the same Apple, Google or email.";
const INVITED = "You've been invited to Spark. Create your account, then download.";

const CONFIG = { priceText: "$20/month", trialDays: 3 };
const LINE = "Free for 3 days, then $20/month. Renews monthly until you cancel.";

// The installer check (GitHub answers a published release asset with a 302) and GET /os/config
// (`config` null: the backend is down).
const installer = (status: number, config: unknown = CONFIG) =>
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (!url.endsWith("/os/config")) return { status };
      if (config === null) throw new Error("down");
      return { ok: true, json: async () => config };
    }),
  );

async function page() {
  render(await OsPage());
}

beforeEach(() => {
  vi.clearAllMocks();
  // Production's API: the short page (the dev-only landing page is in landing.test.tsx).
  vi.stubEnv("SPARK_SHORT_PAGE", "1");
  installer(302);
  s.signedIn = false;
  post.mockResolvedValue({ applied: true });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("/os without an invite", () => {
  it.each(["", "ref=", "ref=a.b", "ref=user_a", `ref=${"x".repeat(65)}`])("%s: the download button only", async (query) => {
    s.query = query;
    await page();
    expect(screen.getByRole("link", { name: DOWNLOAD })).toHaveAttribute("href", "/download/spark");
    expect(screen.queryByTestId("clerk-sign-up")).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it("counts one download click and keeps the plain link", async () => {
    s.query = "";
    await page();
    vi.stubGlobal("location", { hostname: "thelightbulb.company", search: "" });
    fireEvent.click(screen.getByRole("link", { name: DOWNLOAD }));
    expect(capture.mock.calls).toEqual([["spark_download_clicked", { source: "os_page" }]]);
  });

  it("no release yet (404): coming soon, no button", async () => {
    installer(404);
    s.query = "";
    await page();
    expect(screen.getByText("Download coming soon.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: DOWNLOAD })).toBeNull();
  });

  it.each([429, 500])("GitHub answers %s: the download stays", async (status) => {
    installer(status);
    s.query = "";
    await page();
    expect(await screen.findByRole("link", { name: DOWNLOAD })).toBeInTheDocument();
  });
});

describe("/os price line and legal links", () => {
  const legal = () => {
    expect(screen.getByRole("link", { name: "Terms" })).toHaveAttribute("href", "/terms");
    expect(screen.getByRole("link", { name: "Privacy" })).toHaveAttribute("href", "/privacy");
  };

  it.each([
    ["plain", "", false, 302],
    ["coming soon", "", false, 404],
    ["invite, signed out", "ref=abc123XYZ", false, 302],
    ["invite, signed in", "ref=abc123XYZ", true, 302],
  ])("%s: the line from the config and both links", async (_, query, signedIn, status) => {
    installer(status, { priceText: "$25/month", trialDays: 5 });
    s.query = query;
    s.signedIn = signedIn;
    await page();
    expect(await screen.findByText("Free for 5 days, then $25/month. Renews monthly until you cancel.")).toBeInTheDocument();
    legal();
  });

  it.each([null, { priceText: 20 }, { trialDays: 3 }])("config %j unusable: no line, links still shown", async (config) => {
    installer(302, config);
    s.query = "";
    await page();
    expect(screen.queryByText(/Renews monthly/)).toBeNull();
    legal();
  });

  it("asks the backend's public config", async () => {
    s.query = "";
    await page();
    expect(screen.getByText(LINE)).toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls.map(([u]) => String(u))).toContain("http://localhost:8000/api/v1/os/config");
  });
});

describe("/os with an invite", () => {
  beforeEach(() => {
    s.query = "ref=abc123XYZ";
  });

  it("signed out: the invite line and a sign-up that comes back here, no download button", async () => {
    await page();
    expect(screen.getByText(INVITED)).toBeInTheDocument();
    expect(screen.getByTestId("clerk-sign-up")).toBeInTheDocument();
    expect(signUpProps).toHaveBeenCalledWith(
      expect.objectContaining({
        routing: "hash",
        forceRedirectUrl: "/os?ref=abc123XYZ",
        signInForceRedirectUrl: "/os?ref=abc123XYZ",
      }),
    );
    expect(screen.queryByRole("link", { name: DOWNLOAD })).toBeNull();
    expect(screen.getByRole("link", { name: "Skip, just download" })).toHaveAttribute("href", "/download/spark");
    expect(capture.mock.calls).toEqual([["spark_invite_signup_started"]]);
    expect(post).not.toHaveBeenCalled();
  });

  it("signed out before the installer is published: coming soon instead of the skip link", async () => {
    installer(404);
    await page();
    expect(screen.getByTestId("clerk-sign-up")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Skip, just download" })).toBeNull();
    expect(screen.getByText("Download coming soon.")).toBeInTheDocument();
  });

  it("signed in: records the invite once, then the download and the sign-in line", async () => {
    s.signedIn = true;
    await page();
    expect(await screen.findByRole("link", { name: DOWNLOAD })).toBeInTheDocument();
    expect(screen.getByText(SAME)).toBeInTheDocument();
    expect(post.mock.calls).toEqual([["/os/referral", { code: "abc123XYZ" }]]);
    expect(api.get).not.toHaveBeenCalled();
    await waitFor(() => expect(capture.mock.calls).toEqual([["spark_invite_signup_completed"]]));
  });

  it("signing up on the page records the invite without a reload", async () => {
    const el = await OsPage();
    const view = render(el);
    expect(post).not.toHaveBeenCalled();
    s.signedIn = true;
    view.rerender(await OsPage());
    expect(await screen.findByRole("link", { name: DOWNLOAD })).toBeInTheDocument();
    expect(post.mock.calls).toEqual([["/os/referral", { code: "abc123XYZ" }]]);
  });

  it("signed in and the call fails: the download still shows", async () => {
    s.signedIn = true;
    post.mockRejectedValue(new Error("network"));
    await page();
    expect(await screen.findByRole("link", { name: DOWNLOAD })).toBeInTheDocument();
    expect(capture).not.toHaveBeenCalled();
  });
});

it("no /os file calls the account route (it would create the account and start the trial)", () => {
  const dir = join(__dirname, "..");
  for (const f of readdirSync(dir, { recursive: true }) as string[]) {
    if (!/\.tsx?$/.test(f) || f.includes("__tests__")) continue;
    expect(readFileSync(join(dir, f), "utf8"), f).not.toMatch(/os\/account/);
  }
});
