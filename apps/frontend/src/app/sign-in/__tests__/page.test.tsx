// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import Page from "../[[...sign-in]]/page";

const signInProps = vi.hoisted(() => vi.fn());
vi.mock("@clerk/nextjs", () => ({
  SignIn: (props: unknown) => { signInProps(props); return <div data-testid="clerk-sign-in" />; },
}));

it("is Clerk's sign-in-or-up flow under /sign-in, with no link to a /sign-up page", () => {
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
  const { container } = render(<Page />);
  expect(signInProps).toHaveBeenCalledWith(expect.objectContaining({ withSignUp: true, routing: "path", path: "/sign-in" }));
  expect(container.querySelector('a[href^="/sign-up"]')).toBeNull();
});
