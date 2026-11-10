import { expect, it } from "vitest";
import { windowLabel, withDefaults } from "../data";

it("a waitlist window shows with one end or both", () => {
  expect(windowLabel("14:00", "16:00")).toBe("14:00–16:00");
  expect(windowLabel("14:00", "")).toBe("after 14:00");
  expect(windowLabel("", "12:00")).toBe("before 12:00");
  expect(windowLabel("", "")).toBe("");
});

it("a config saved before allow.waitlist existed reads it as on, like the server", () => {
  const c = withDefaults({ allow: { book: false } } as never);
  expect(c.allow).toEqual({ book: false, move: true, waitlist: true, greetByName: true, answer: true });
  expect(withDefaults(null).allow.waitlist).toBe(true);
});
