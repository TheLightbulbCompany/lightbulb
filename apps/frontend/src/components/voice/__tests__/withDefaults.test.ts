import { expect, it } from "vitest";
import { withDefaults } from "../data";

it("a config saved before allow.waitlist existed reads it as on, like the server", () => {
  const c = withDefaults({ allow: { book: false } } as never);
  expect(c.allow).toEqual({ book: false, move: true, waitlist: true, greetByName: true, answer: true });
  expect(withDefaults(null).allow.waitlist).toBe(true);
});
