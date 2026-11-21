// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import SparkSignInPage from "../page";

const s = vi.hoisted(() => ({
  signedIn: true,
  query: "scheme=spark",
  post: vi.fn(),
  signInProps: vi.fn(),
}));
const api = vi.hoisted(() => ({ post: (...args: unknown[]) => s.post(...args) }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(s.query) }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: s.signedIn }),
  SignIn: (props: unknown) => {
    s.signInProps(props);
    return <div>sign in</div>;
  },
}));

const original = window.location;
beforeEach(() => {
  Object.assign(s, { signedIn: true, query: "scheme=spark" });
  vi.clearAllMocks();
  s.post.mockResolvedValue({ ticket: "user_a.secret" });
  Object.defineProperty(window, "location", { configurable: true, value: { href: "" } });
});
afterEach(() => {
  Object.defineProperty(window, "location", { configurable: true, value: original });
});

describe("Spark sign-in", () => {
  it.each(["spark", "spark-dev", "eve"])("%s: asks for a ticket and opens the app with it", async (scheme) => {
    s.query = `scheme=${scheme}`;
    render(<SparkSignInPage />);
    expect(await screen.findByText("You can go back to Spark")).toBeInTheDocument();
    expect(s.post).toHaveBeenCalledWith("/os/ticket", {});
    expect(window.location.href).toBe(`${scheme}://auth?ticket=user_a.secret`);
    Object.assign(window.location, { href: "" });
    fireEvent.click(screen.getByRole("button", { name: "Open Spark" }));
    expect(window.location.href).toBe(`${scheme}://auth?ticket=user_a.secret`);
  });

  it.each(["scheme=javascript", "scheme=https", "scheme=", "", "scheme=spark://evil"])(
    "%s: refused, nothing called or opened",
    async (query) => {
      s.query = query;
      render(<SparkSignInPage />);
      expect(screen.getByText("This sign-in link is not valid. Start again from Spark.")).toBeInTheDocument();
      expect(s.post).not.toHaveBeenCalled();
      expect(window.location.href).toBe("");
    },
  );

  it("signed out shows sign-in that returns to the same link", () => {
    s.signedIn = false;
    s.query = "scheme=spark-dev";
    render(<SparkSignInPage />);
    expect(screen.getByText("sign in")).toBeInTheDocument();
    expect(s.signInProps).toHaveBeenCalledWith(
      expect.objectContaining({
        forceRedirectUrl: "/auth/spark?scheme=spark-dev",
        signUpForceRedirectUrl: "/auth/spark?scheme=spark-dev",
      }),
    );
    expect(s.post).not.toHaveBeenCalled();
  });

  it("a failed ticket says so and opens nothing", async () => {
    s.post.mockRejectedValue(new Error("boom"));
    render(<SparkSignInPage />);
    expect(await screen.findByText("Could not sign in. Reload the page to try again.")).toBeInTheDocument();
    expect(window.location.href).toBe("");
  });
});
