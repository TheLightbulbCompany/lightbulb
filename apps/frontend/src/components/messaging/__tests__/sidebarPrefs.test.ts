import { expect, it } from "vitest";
import { clampWidth, readSessionColumnWidth, readSidebarPrefs, writeSessionColumnWidth } from "../sidebarPrefs";

it("round-trips a valid session column width and drops a bad one, in its own key", () => {
  writeSessionColumnWidth("org", 480);
  expect(readSessionColumnWidth("org")).toBe(480);
  window.localStorage.setItem("lightbulb.messaging.sessionColumnWidth.org", "wide");
  expect(readSessionColumnWidth("org")).toBeUndefined();
  window.localStorage.setItem("lightbulb.messaging.sessionColumnWidth.org", "12");
  expect(readSessionColumnWidth("org")).toBeUndefined();
});

it("lets the 80% ceiling undercut the 360px floor on a narrow panel", () => {
  // At 400px wide, 80% is 320 -- below the 360px floor. The middle pane must
  // keep at least 20%, so the ceiling wins over the floor.
  expect(clampWidth(1000, 400)).toBe(320);
});

it("defaults to recent activity, and reads the old auto-written alpha default as recent", () => {
  window.localStorage.clear();
  expect(readSidebarPrefs("org").sort).toBe("recent");
  window.localStorage.setItem("lightbulb.messaging.sidebar.org", JSON.stringify({ collapsed: [], sort: "alpha" }));
  expect(readSidebarPrefs("org").sort).toBe("recent");
  window.localStorage.setItem("lightbulb.messaging.sidebar.org", JSON.stringify({ collapsed: [], sort: "name" }));
  expect(readSidebarPrefs("org").sort).toBe("name");
});
