// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useSessionJoinRequest } from "./sessionControls";
import type { LocalSession } from "./types";

afterEach(cleanup);
const session = (c: Partial<LocalSession> = {}) => ({ sessionId: "s1", status: "busy", ...c }) as LocalSession;

const declined = session({ pickupRequest: "declined" });
const plain = session();

describe("useSessionJoinRequest kind", () => {
  it("asks to pick up through the pick-up door and reflects pickupRequest", async () => {
    const pick = vi.fn().mockResolvedValue(session({ pickupRequest: "pending" }));
    const join = vi.fn();
    const client = { submitLocalSessionCommand: vi.fn(), requestLocalSessionPickupAccess: pick, requestLocalSessionJoin: join };
    const { result } = renderHook(() => useSessionJoinRequest(client, declined, "pickup"));
    expect(result.current.state).toBe("declined");
    await act(async () => { await result.current.ask(); });
    expect(pick).toHaveBeenCalledWith("s1");
    expect(join).not.toHaveBeenCalled();
    expect(result.current.state).toBe("pending");
  });

  it("stays on the join door by default", async () => {
    const join = vi.fn().mockResolvedValue(session({ joinRequest: "pending" }));
    const client = { submitLocalSessionCommand: vi.fn(), requestLocalSessionJoin: join };
    const { result } = renderHook(() => useSessionJoinRequest(client, plain));
    await act(async () => { await result.current.ask(); });
    expect(join).toHaveBeenCalledWith("s1");
    expect(result.current.state).toBe("pending");
  });
});
