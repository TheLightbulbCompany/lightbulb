import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { useChannelLinks } from "../useChannelLinks";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
}));

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>
);

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

function bot(overrides: Partial<{ agent_id: string; bot_username: string; linked: boolean }> = {}) {
  return { agent_id: "agent_1", bot_username: "lightbulb_bot", linked: false, ...overrides };
}

describe("useChannelLinks", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("does not fetch when disabled (null SWR key)", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      jsonResponse({ telegram: [], discord: [], slack: [], can_create_bots: true }),
    );
    global.fetch = fetchMock;

    const { result } = renderHook(() => useChannelLinks(false), { wrapper });

    // Give any (incorrect) fetch a chance to fire before asserting it didn't.
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.channelLinks).toBeUndefined();
    expect(result.current.channelLinked).toBe(false);
  });

  it("fetches /channels/links/me when enabled", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      jsonResponse({ telegram: [], discord: [], slack: [], can_create_bots: true }),
    );
    global.fetch = fetchMock;

    renderHook(() => useChannelLinks(true), { wrapper });

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/channels/links/me"),
        expect.anything(),
      ),
    );
  });

  it("derives channelLinked=false when no bot across telegram/discord/slack is linked", async () => {
    global.fetch = vi.fn().mockImplementation(() =>
      jsonResponse({
        telegram: [bot({ linked: false })],
        discord: [bot({ agent_id: "agent_2", linked: false })],
        slack: [],
        can_create_bots: true,
      }),
    );

    const { result } = renderHook(() => useChannelLinks(true), { wrapper });

    await waitFor(() => expect(result.current.channelLinks).toBeDefined());
    expect(result.current.channelLinked).toBe(false);
  });

  it.each(["telegram", "discord", "slack"] as const)(
    "derives channelLinked=true when a %s bot is linked",
    async (provider) => {
      global.fetch = vi.fn().mockImplementation(() =>
        jsonResponse({
          telegram: [],
          discord: [],
          slack: [],
          can_create_bots: true,
          [provider]: [bot({ linked: true })],
        }),
      );

      const { result } = renderHook(() => useChannelLinks(true), { wrapper });

      await waitFor(() => expect(result.current.channelLinked).toBe(true));
    },
  );

  it("exposes refresh (SWR mutate) that refetches on demand", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      jsonResponse({ telegram: [], discord: [], slack: [], can_create_bots: true }),
    );
    global.fetch = fetchMock;

    const { result } = renderHook(() => useChannelLinks(true), { wrapper });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await result.current.refresh();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });
});
