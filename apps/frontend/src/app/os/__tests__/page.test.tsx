// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import OsPage from "../page";

const s = vi.hoisted(() => ({ query: "" }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(s.query) }));
const capture = vi.hoisted(() => vi.fn());
vi.mock("@/lib/analytics", () => ({ capture }));
vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));

const INVITE = "Open Spark to apply this invite";

describe("/os invite link", () => {
  it("a valid referral code links to the app with it", () => {
    s.query = "ref=abc123XYZ";
    vi.stubGlobal("location", { hostname: "thelightbulb.company" });
    render(<OsPage />);
    expect(screen.getByRole("link", { name: INVITE })).toHaveAttribute("href", "spark://referral?code=abc123XYZ");
  });

  it("the dev site links to the dev build", () => {
    s.query = "ref=abc123XYZ";
    vi.stubGlobal("location", { hostname: "dev.thelightbulb.company" });
    render(<OsPage />);
    expect(screen.getByRole("link", { name: INVITE })).toHaveAttribute("href", "spark-dev://referral?code=abc123XYZ");
    vi.unstubAllGlobals();
  });

  it.each(["", "ref=", "ref=a.b", "ref=user_a", "ref=%3Cx%3E", `ref=${"x".repeat(65)}`])("%s: no invite link", (query) => {
    s.query = query;
    render(<OsPage />);
    expect(screen.queryByRole("link", { name: INVITE })).toBeNull();
  });
});

describe("/os download click", () => {
  it.each([
    ["", { source: "os_page" }],
    ["?ref=abc123XYZ", { source: "os_page", has_referral: true }],
  ])("%s: counts one click and keeps the plain link", (search, props) => {
    capture.mockClear();
    vi.stubGlobal("location", { hostname: "thelightbulb.company", search });
    render(<OsPage />);
    const link = screen.getByRole("link", { name: "Download for Mac" });
    fireEvent.click(link);
    expect(capture).toHaveBeenCalledTimes(1);
    expect(capture).toHaveBeenCalledWith("spark_download_clicked", props);
    expect(link).toHaveAttribute("href", "/download/spark");
    vi.unstubAllGlobals();
  });
});
