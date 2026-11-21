// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { AuthSwitchLink } from "../AuthSwitchLink";

const q = vi.hoisted(() => ({ search: "" }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(q.search) }));

describe("AuthSwitchLink", () => {
  it("carries redirect_url to the other auth page", () => {
    q.search = "redirect_url=https%3A%2F%2Fapp.test%2Fauth%2Fspark%3Fscheme%3Dspark";
    render(<AuthSwitchLink to="/sign-up">Sign up</AuthSwitchLink>);
    expect(screen.getByRole("link", { name: "Sign up" })).toHaveAttribute(
      "href",
      "/sign-up?redirect_url=https%3A%2F%2Fapp.test%2Fauth%2Fspark%3Fscheme%3Dspark",
    );
  });

  it("is the plain link without one", () => {
    q.search = "";
    render(<AuthSwitchLink to="/sign-in">Sign in</AuthSwitchLink>);
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
  });
});
