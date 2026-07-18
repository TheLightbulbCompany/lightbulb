import { renderHook, waitFor } from "@testing-library/react-native";

import { FIRST_RUN_PROMPT } from "../../../lib/firstRun";
import { useFirstRunKickoff } from "../useFirstRunKickoff";

function makeUser(firstRun: Record<string, unknown>) {
  return {
    unsafeMetadata: { firstRun },
    update: jest.fn(async () => undefined),
  } as never;
}

describe("useFirstRunKickoff", () => {
  it("stamps started THEN sends the visible prompt, exactly once", async () => {
    const user = makeUser({ briefSubmitted: true });
    const send = jest.fn(() => true);
    renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "agent_friday",
        defaultId: "agent_friday",
        isConnected: true,
        send,
      }),
    );

    await waitFor(() => expect(send).toHaveBeenCalledWith(FIRST_RUN_PROMPT));
    const update = (user as { update: jest.Mock }).update;
    expect(update).toHaveBeenCalledWith({
      unsafeMetadata: { firstRun: { briefSubmitted: true, started: true } },
    });
    // Write-before-send ordering is load-bearing (see hook docstring).
    expect(update.mock.invocationCallOrder[0]).toBeLessThan(
      send.mock.invocationCallOrder[0],
    );
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("does nothing for a non-default agent", async () => {
    const user = makeUser({ briefSubmitted: true });
    const send = jest.fn(() => true);
    renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "agent_other",
        defaultId: "agent_friday",
        isConnected: true,
        send,
      }),
    );
    await waitFor(() => expect(send).not.toHaveBeenCalled());
  });

  it("does nothing when the kickoff already started, or without a brief", async () => {
    const started = makeUser({ briefSubmitted: true, started: true });
    const noBrief = makeUser({});
    const send = jest.fn(() => true);
    renderHook(() =>
      useFirstRunKickoff({
        user: started,
        agentId: "a",
        defaultId: "a",
        isConnected: true,
        send,
      }),
    );
    renderHook(() =>
      useFirstRunKickoff({
        user: noBrief,
        agentId: "a",
        defaultId: "a",
        isConnected: true,
        send,
      }),
    );
    await waitFor(() => expect(send).not.toHaveBeenCalled());
  });
});
