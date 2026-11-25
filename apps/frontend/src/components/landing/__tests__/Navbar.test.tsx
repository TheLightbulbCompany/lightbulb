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

import { Navbar, navCta, PRODUCTS } from "../Navbar";

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

  it("puts the three products under one Products menu, in order, then Pricing", () => {
    render(<Navbar />);
    const products = screen.getByRole("button", { name: "Products" });
    expect(products).toHaveAttribute("aria-controls", "nav-products-panel");
    const panel = document.getElementById("nav-products-panel")!;
    const links = [...panel.querySelectorAll("a")];
    expect(links.map((a) => [a.getAttribute("href"), a.querySelector(".nav-product-name")?.textContent])).toEqual([
      ["/phone", "Lightbulb Phone"],
      ["/meetings", "Lightbulb Meetings"],
      ["/os", "Spark"],
    ]);
    expect(links[2].textContent).toContain("One-click voice interface for your Mac.");
    expect(PRODUCTS).toHaveLength(3);
    // No product is a top-level link of its own any more.
    const top = [...document.querySelectorAll(".nav-links > a")].map((a) => a.getAttribute("href"));
    expect(top).toContain("/pricing");
    expect(top.filter((h) => ["/phone", "/meetings", "/os"].includes(h ?? ""))).toEqual([]);
  });

  it("opens and closes the Products panel: click, Escape, outside click, link tap", () => {
    render(<Navbar />);
    const products = screen.getByRole("button", { name: "Products" });
    const panel = document.getElementById("nav-products-panel")!;
    expect(products).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(products);
    expect(products).toHaveAttribute("aria-expanded", "true");
    expect(panel).toHaveClass("open");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(products).toHaveAttribute("aria-expanded", "false");
    expect(products).toHaveFocus();
    fireEvent.click(products);
    fireEvent.pointerDown(document.body);
    expect(products).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(products);
    fireEvent.click(screen.getByRole("link", { name: /Lightbulb Meetings/ }));
    expect(products).toHaveAttribute("aria-expanded", "false");
  });

  it("closes the Products panel on a route change", () => {
    const { rerender } = render(<Navbar />);
    const products = screen.getByRole("button", { name: "Products" });
    fireEvent.click(products);
    s.path = "/pricing";
    rerender(<Navbar />);
    expect(products).toHaveAttribute("aria-expanded", "false");
  });

  it("marks the current product, and only it", () => {
    s.path = "/meetings";
    render(<Navbar />);
    expect(screen.getByRole("link", { name: /Lightbulb Meetings/ })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: /Lightbulb Phone/ })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: /Spark/ })).not.toHaveAttribute("aria-current");
  });

  it("closes the phone menu after a product tap", () => {
    render(<Navbar />);
    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));
    expect(document.querySelector(".nav-links")).toHaveClass("mobile-open");
    fireEvent.click(screen.getByRole("link", { name: /Spark/ }));
    expect(document.querySelector(".nav-links")).not.toHaveClass("mobile-open");
  });
});
