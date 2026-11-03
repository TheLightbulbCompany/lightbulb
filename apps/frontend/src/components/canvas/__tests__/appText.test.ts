import { describe, expect, it } from "vitest";
import type { AppRow } from "@/hooks/useApps";
import { statusLine } from "../appText";

const app = (over: Partial<AppRow> = {}): AppRow => ({
  app_id: "a1", name: "Invoices", room_id: "r1", room_name: "finance", state: "building", open_to: "room",
  budget_usd: 20, preview_url: null, live_url: null, live_version: null, last_opened_at: null,
  spend_month_cents: 0, can_open: true, ...over,
});

describe("statusLine", () => {
  it("a stuck app says where it stopped, whatever its state", () => {
    for (const state of ["building", "provisioning", "ready"] as const) {
      expect(statusLine(app({ state, stuck: { at: "2026-11-03T00:00:00Z", step: "setting up hosting" } })))
        .toBe("Stopped at setting up hosting. Nothing has moved for 5 minutes.");
    }
  });

  it("without stuck, the usual line", () => {
    expect(statusLine(app({ stuck: null }))).toBe("The agent is making the preview. It'll post in #finance when it's ready.");
    expect(statusLine(app({ state: "ready" }))).toBeNull();
  });
});
