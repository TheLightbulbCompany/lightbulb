// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const s = vi.hoisted(() => ({ path: "/", signedIn: false }));
vi.mock("next/navigation", () => ({ usePathname: () => s.path }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: s.signedIn }),
  UserButton: () => null,
}));
const captureSignupCta = vi.hoisted(() => vi.fn());
vi.mock("@/lib/signupFlow", () => ({ captureSignupCta }));
const capture = vi.hoisted(() => vi.fn());
vi.mock("@/lib/analytics", () => ({ capture }));

import { Navbar, navCta } from "../Navbar";

const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1";

// The filled button in the bar (the phone menu carries a second copy of it).
const button = () => document.querySelector(".nav-right .btn-primary") as HTMLAnchorElement;

beforeEach(() => {
  vi.clearAllMocks();
  s.path = "/";
  s.signedIn = false;
});
afterEach(() => vi.restoreAllMocks());

describe("navCta", () => {
  it("maps each page to its product's button", () => {
    expect(navCta("/os")).toMatchObject({ product: "spark", label: "Download for Mac", href: "/download/spark" });
    expect(navCta("/os/billing").product).toBe("spark");
    expect(navCta("/meetings")).toMatchObject({ label: "Join the waitlist", href: "/meetings#waitlist" });
    for (const p of ["/", "/phone", "/pricing", "/compare/x", "/osx"]) {
      expect(navCta(p)).toMatchObject({ product: "phone", label: "Set up Lightbulb Phone", href: "/sign-up" });
    }
  });
});

describe("Navbar", () => {
  it("on Spark pages: Download for Mac, the Spark download event, no Log in, even when signed in", () => {
    s.path = "/os";
    s.signedIn = true;
    render(<Navbar />);
    expect(button()).toHaveAttribute("href", "/download/spark");
    expect(button().textContent).toContain("Download for Mac");
    expect(screen.queryByRole("link", { name: "Log in" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Open Lightbulb" })).toBeNull();
    fireEvent.click(button());
    expect(capture).toHaveBeenCalledWith("spark_download_clicked", { source: "nav" });
    expect(captureSignupCta).not.toHaveBeenCalled();
  });

  it("on a phone the Spark button goes to the page's Mac line and starts no download", () => {
    vi.spyOn(navigator, "userAgent", "get").mockReturnValue(IPHONE);
    s.path = "/os";
    render(<Navbar />);
    expect(button()).toHaveAttribute("href", "#get");
    fireEvent.click(button());
    expect(capture).not.toHaveBeenCalled();
    s.path = "/os/billing";
    render(<Navbar />);
    expect(document.querySelectorAll(".nav-right .btn-primary")[1]).toHaveAttribute("href", "/os#get");
  });

  it("on Meetings: Join the waitlist, to the page's form", () => {
    s.path = "/meetings";
    render(<Navbar />);
    expect(button()).toHaveAttribute("href", "/meetings#waitlist");
    expect(button().textContent).toContain("Join the waitlist");
    expect(screen.getAllByRole("link", { name: "Log in" }).length).toBeGreaterThan(0);
    fireEvent.click(button());
    expect(captureSignupCta).not.toHaveBeenCalled();
  });

  it("everywhere else: unchanged Lightbulb Phone sign-up", () => {
    s.path = "/phone";
    render(<Navbar />);
    expect(button()).toHaveAttribute("href", "/sign-up");
    expect(button().textContent).toContain("Set up Lightbulb Phone");
    fireEvent.click(button());
    expect(captureSignupCta).toHaveBeenCalledWith({
      ctaId: "nav_sign_up",
      label: "Set up Lightbulb Phone",
      surface: "landing_nav",
    });
    expect(capture).not.toHaveBeenCalled();
  });
});
