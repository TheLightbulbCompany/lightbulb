import { describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { capture } from "@/lib/analytics";
import { useFirstRunKickoff } from "@/hooks/useFirstRunKickoff";
import { FIRST_RUN_PROMPT } from "@/lib/firstRun";

function fakeUser(
  firstRun: Record<string, unknown>,
  update = vi.fn().mockResolvedValue(undefined),
) {
  return {
    unsafeMetadata: { firstRun },
    update,
  } as never;
}

describe("useFirstRunKickoff", () => {
  it("sends the prompt once when brief submitted and not started", async () => {
    const sendMessage = vi.fn().mockResolvedValue(undefined);
    const user = fakeUser({ briefSubmitted: true });
    const { rerender } = renderHook(() =>
      useFirstRunKickoff({ user, agentId: "a1", defaultId: "a1", sendMessage }),
    );
    await waitFor(() => expect(sendMessage).toHaveBeenCalledWith(FIRST_RUN_PROMPT));
    rerender();
    expect(sendMessage).toHaveBeenCalledTimes(1);
  });

  it("does nothing when already started", () => {
    const sendMessage = vi.fn();
    const user = fakeUser({ briefSubmitted: true, started: true });
    renderHook(() =>
      useFirstRunKickoff({ user, agentId: "a1", defaultId: "a1", sendMessage }),
    );
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("does nothing on a non-default agent window", () => {
    const sendMessage = vi.fn();
    const user = fakeUser({ briefSubmitted: true });
    renderHook(() =>
      useFirstRunKickoff({ user, agentId: "a2", defaultId: "a1", sendMessage }),
    );
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("does not send or capture when the metadata write rejects", async () => {
    const sendMessage = vi.fn().mockResolvedValue(undefined);
    const update = vi.fn().mockRejectedValue(new Error("network down"));
    const user = fakeUser({ briefSubmitted: true }, update);
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    renderHook(() =>
      useFirstRunKickoff({ user, agentId: "a1", defaultId: "a1", sendMessage }),
    );

    // Wait a tick for the rejected write's catch handler to run.
    await waitFor(() =>
      expect(errorSpy).toHaveBeenCalledWith("first-run kickoff failed", expect.any(Error)),
    );
    expect(sendMessage).not.toHaveBeenCalled();
    expect(capture).not.toHaveBeenCalled();

    errorSpy.mockRestore();
  });
});
