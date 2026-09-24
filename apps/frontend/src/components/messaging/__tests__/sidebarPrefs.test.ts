import { expect, it } from "vitest";
import { readSidebarPrefs } from "../sidebarPrefs";

it("defaults to recent activity, and reads the old auto-written alpha default as recent", () => {
  window.localStorage.clear();
  expect(readSidebarPrefs("org").sort).toBe("recent");
  window.localStorage.setItem("lightbulb.messaging.sidebar.org", JSON.stringify({ collapsed: [], sort: "alpha" }));
  expect(readSidebarPrefs("org").sort).toBe("recent");
  window.localStorage.setItem("lightbulb.messaging.sidebar.org", JSON.stringify({ collapsed: [], sort: "name" }));
  expect(readSidebarPrefs("org").sort).toBe("name");
});
