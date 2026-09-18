import { describe, it, expect } from "vitest";
import { SETTINGS_NAV_SECTIONS, filterNavSectionsForRuntime } from "../settings-nav-config";

function itemIds(sections: ReturnType<typeof filterNavSectionsForRuntime>): string[] {
  return sections.flatMap((section) => section.items.map((item) => item.id));
}

describe("filterNavSectionsForRuntime", () => {
  it("keeps every row while the host is unknown", () => {
    const ids = itemIds(filterNavSectionsForRuntime(SETTINGS_NAV_SECTIONS, false));
    expect(ids).toContain("channels");
    expect(ids).toContain("channel-status");
  });

  // H3: the WIRING "Channels" row (MyChannelsSection) has no Hermes equivalent
  // and must drop out alongside the ENGINE ROOM "Channel Status" row once
  // AgentCore is confirmed — they are two distinct ids (H6), both hidden.
  it("hides the channels row (and channel-status, and dreams) once AgentCore is confirmed", () => {
    const ids = itemIds(filterNavSectionsForRuntime(SETTINGS_NAV_SECTIONS, true));
    expect(ids).not.toContain("channels");
    expect(ids).not.toContain("channel-status");
    expect(ids).not.toContain("dreams");
    // Sibling WIRING rows stay.
    expect(ids).toContain("mcp");
    expect(ids).toContain("webhooks");
  });
});
