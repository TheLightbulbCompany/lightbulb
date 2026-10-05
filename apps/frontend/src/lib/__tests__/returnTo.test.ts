import { expect, it } from "vitest";
import { safeReturnTo } from "../returnTo";

it("keeps only same-origin paths", () => {
  expect(safeReturnTo("/s?port=5000")).toBe("/s?port=5000");
  for (const bad of [null, "", "https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)"]) {
    expect(safeReturnTo(bad)).toBe("/chat");
  }
});
