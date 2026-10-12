// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({ useApi: () => ({ post: vi.fn() }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ onEvent: () => () => undefined, claimFrames: () => () => undefined }) }));

import { showsBrowserTab } from "../SessionBrowserTab";

describe("showsBrowserTab", () => {
  it("shows only while a running session watched from elsewhere has a browser connection open", () => {
    expect(showsBrowserTab({ browser: "mac", status: "busy" }, true)).toBe(true);
    expect(showsBrowserTab({ browser: "cloud", status: "waiting-approval" }, true)).toBe(true);
    expect(showsBrowserTab({ browser: null, status: "busy" }, true)).toBe(false);
    expect(showsBrowserTab({ browser: "mac", status: "ended" }, true)).toBe(false);
    expect(showsBrowserTab({ browser: "mac", status: "offline" }, true)).toBe(false);
    expect(showsBrowserTab({ browser: "mac", status: "busy" }, false)).toBe(false);
  });
});
