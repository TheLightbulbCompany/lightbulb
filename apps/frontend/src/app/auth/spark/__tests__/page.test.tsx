// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import SparkSignInPage from "../page";

const s = vi.hoisted(() => ({
  signedIn: true,
  query: "scheme=spark",
  post: vi.fn(),
  redirectToSignIn: vi.fn(),
}));
const clerk = vi.hoisted(() => ({ redirectToSignIn: (...args: unknown[]) => s.redirectToSignIn(...args) }));
const api = vi.hoisted(() => ({ post: (...args: unknown[]) => s.post(...args) }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(s.query) }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: s.signedIn }),
  useClerk: () => clerk,
}));

const STATE = "app-state_0123456789";
const original = window.location;
beforeEach(() => {
  Object.assign(s, { signedIn: true, query: `scheme=spark&state=${STATE}` });
  vi.clearAllMocks();
  localStorage.clear();
  s.post.mockResolvedValue({ ticket: "user_a.secret" });
  Object.defineProperty(window, "location", { configurable: true, value: { href: "", origin: "https://app.test" } });
});
afterEach(() => {
  Object.defineProperty(window, "location", { configurable: true, value: original });
});

describe("Spark sign-in", () => {
  it.each(["spark", "spark-dev", "eve"])("%s: asks for a ticket and opens the app with it", async (scheme) => {
    s.query = `scheme=${scheme}&state=${STATE}`;
    render(<SparkSignInPage />);
    expect(await screen.findByText("You can go back to Spark")).toBeInTheDocument();
    expect(s.post).toHaveBeenCalledWith("/os/ticket", { state: STATE });
    const opened = `${scheme}://auth?ticket=user_a.secret&state=${STATE}`;
    expect(window.location.href).toBe(opened);
    Object.assign(window.location, { href: "" });
    fireEvent.click(screen.getByRole("button", { name: "Open Spark" }));
    expect(window.location.href).toBe(opened);
  });

  it.each([
    `scheme=javascript&state=${STATE}`,
    `scheme=https&state=${STATE}`,
    `scheme=&state=${STATE}`,
    "",
    `scheme=spark://evil&state=${STATE}`,
    "scheme=spark",
    "scheme=spark&state=",
    "scheme=spark&state=tooshort",
    `scheme=spark&state=${"a".repeat(129)}`,
    "scheme=spark&state=has%20a%20space%20in%20it",
    "scheme=spark&state=bad%26state%3Dinjected01",
  ])(
    "%s: refused, nothing called or opened",
    async (query) => {
      s.query = query;
      render(<SparkSignInPage />);
      expect(screen.getByText("This sign-in link is not valid. Start again from Spark.")).toBeInTheDocument();
      expect(s.post).not.toHaveBeenCalled();
      expect(window.location.href).toBe("");
    },
  );

  it("signed out goes to the app's sign-in page, which returns to the same link", () => {
    s.signedIn = false;
    s.query = `scheme=spark-dev&state=${STATE}`;
    render(<SparkSignInPage />);
    expect(s.redirectToSignIn).toHaveBeenCalledWith({
      redirectUrl: `https://app.test/auth/spark?scheme=spark-dev&state=${STATE}`,
    });
    expect(s.post).not.toHaveBeenCalled();
  });

  it("signed out with a bad state shows the error and goes nowhere", () => {
    s.signedIn = false;
    s.query = "scheme=spark&state=short";
    render(<SparkSignInPage />);
    expect(screen.getByText("This sign-in link is not valid. Start again from Spark.")).toBeInTheDocument();
    expect(s.redirectToSignIn).not.toHaveBeenCalled();
  });

  it("signed out with a bad scheme shows the error and goes nowhere", () => {
    s.signedIn = false;
    s.query = `scheme=javascript&state=${STATE}`;
    render(<SparkSignInPage />);
    expect(screen.getByText("This sign-in link is not valid. Start again from Spark.")).toBeInTheDocument();
    expect(s.redirectToSignIn).not.toHaveBeenCalled();
  });

  it("one page load asks for one ticket, even under strict mode", async () => {
    render(<StrictMode><SparkSignInPage /></StrictMode>);
    expect(await screen.findByText("You can go back to Spark")).toBeInTheDocument();
    expect(s.post).toHaveBeenCalledTimes(1);
  });

  it("sends a stored referral code with the ticket request and clears it after", async () => {
    localStorage.setItem("lightbulb.os.ref", "abc123");
    render(<SparkSignInPage />);
    expect(await screen.findByText("You can go back to Spark")).toBeInTheDocument();
    expect(s.post).toHaveBeenCalledWith("/os/ticket", { state: STATE, ref: "abc123" });
    expect(localStorage.getItem("lightbulb.os.ref")).toBeNull();
  });

  it("keeps the stored code when the ticket request fails", async () => {
    localStorage.setItem("lightbulb.os.ref", "abc123");
    s.post.mockRejectedValue(new Error("boom"));
    render(<SparkSignInPage />);
    expect(await screen.findByText("Could not sign in. Reload the page to try again.")).toBeInTheDocument();
    expect(localStorage.getItem("lightbulb.os.ref")).toBe("abc123");
  });

  it("a failed ticket says so and opens nothing", async () => {
    s.post.mockRejectedValue(new Error("boom"));
    render(<SparkSignInPage />);
    expect(await screen.findByText("Could not sign in. Reload the page to try again.")).toBeInTheDocument();
    expect(window.location.href).toBe("");
  });
});
