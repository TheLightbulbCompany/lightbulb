import { act, render, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ isLoaded: false, userId: null as string | null }));
const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => auth }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import { useTextChats } from "../data";

function Chats() { const { data, error } = useTextChats(); return <p>{error ? `error: ${String(error)}` : data?.map((c) => c.roomId).join(",") ?? "loading"}</p>; }

beforeEach(() => {
  Object.assign(auth, { isLoaded: false, userId: null });
  api.get.mockReset().mockResolvedValue({ rooms: [{ roomId: "dm", kind: "dm", members: [{ kind: "agent", id: "a" }, { kind: "human", id: "user_x" }], updatedAt: "2026-11-20T00:00:00Z" }] });
});

describe("a cold load of /voice (Clerk not loaded on the first render)", () => {
  it("lists the chats once Clerk loads, with no throw", async () => {
    const view = render(<SWRConfig value={{ provider: () => new Map() }}><Chats /></SWRConfig>);
    expect(view.container.textContent).toBe("loading");
    await act(async () => { Object.assign(auth, { isLoaded: true, userId: "user_x" }); view.rerender(<SWRConfig value={{ provider: () => new Map() }}><Chats /></SWRConfig>); });
    await waitFor(() => expect(view.container.textContent).toBe("dm"));
  });
});
