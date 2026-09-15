import { expect, it } from "vitest";
import { readSidebarPrefs, writeSidebarPrefs } from "../sidebarPrefs";

it("round-trips a valid session column width and drops a bad one", () => {
  writeSidebarPrefs("org", { collapsed: [], sort: "alpha", sessionColumnWidth: 480 });
  expect(readSidebarPrefs("org").sessionColumnWidth).toBe(480);
  window.localStorage.setItem("lightbulb.messaging.sidebar.org", JSON.stringify({ collapsed: [], sort: "alpha", sessionColumnWidth: "wide" }));
  expect(readSidebarPrefs("org").sessionColumnWidth).toBeUndefined();
  window.localStorage.setItem("lightbulb.messaging.sidebar.org", JSON.stringify({ collapsed: [], sort: "alpha", sessionColumnWidth: 12 }));
  expect(readSidebarPrefs("org").sessionColumnWidth).toBeUndefined();
});
