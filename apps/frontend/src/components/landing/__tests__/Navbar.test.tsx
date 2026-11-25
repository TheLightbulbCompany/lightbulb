// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const s = vi.hoisted(() => ({ path: "/" }));
vi.mock("next/navigation", () => ({ usePathname: () => s.path }));
const capture = vi.hoisted(() => vi.fn());
vi.mock("@/lib/analytics", () => ({ capture }));

import { Navbar } from "../Navbar";

const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1";

// The filled button in the bar (the phone menu carries a second copy of it).
const button = () => document.querySelector(".nav-right .btn-primary") as HTMLAnchorElement;

beforeEach(() => {
  vi.clearAllMocks();
  s.path = "/";
});
afterEach(() => vi.restoreAllMocks());

describe("Navbar", () => {
  it("is Spark only: one Spark link, Download for Mac, no Log in or other products", () => {
    render(<Navbar />);
    expect(button()).toHaveAttribute("href", "/download/spark");
    expect(button().textContent).toContain("Download for Mac");
    expect(screen.queryByRole("link", { name: "Log in" })).toBeNull();
    const top = [...document.querySelectorAll(".nav-links > a")].map((a) => a.getAttribute("href"));
    expect(top).toEqual(["/os", "/download/spark"]);
    fireEvent.click(button());
    expect(capture).toHaveBeenCalledWith("spark_download_clicked", { source: "nav" });
  });

  it("on a phone the button goes to the page's Mac line and starts no download", () => {
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

  it("closes the phone menu after a tap", () => {
    render(<Navbar />);
    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));
    expect(document.querySelector(".nav-links")).toHaveClass("mobile-open");
    fireEvent.click(screen.getByRole("link", { name: "Spark" }));
    expect(document.querySelector(".nav-links")).not.toHaveClass("mobile-open");
  });
});
