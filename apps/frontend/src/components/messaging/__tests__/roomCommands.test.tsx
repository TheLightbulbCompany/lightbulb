import { renderHook } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { useRoomCommandResults } from "../roomCommands";
import { activateMessagesPanel } from "@/components/workspace/workspaceController";
import { useMessagingWorkspaceOptional } from "../MessagingWorkspaceContext";

vi.mock("@/components/workspace/workspaceController", () => ({ activateMessagesPanel: vi.fn() }));
vi.mock("../MessagingWorkspaceContext", () => ({ useMessagingWorkspaceOptional: vi.fn() }));

// Explicit per-test setup, not the mock factory's implicit `undefined`
// default — a test-3-sets-it-late file-order dependency once made tests 1-2
// pass only by accident of running before test 3.
beforeEach(() => {
  vi.mocked(useMessagingWorkspaceOptional).mockReturnValue(undefined as never);
});

it("returns nothing when no messaging provider is mounted", () => {
  const { result } = renderHook(() => useRoomCommandResults("", true));
  expect(result.current).toEqual([]);
});

it("returns nothing when the palette is closed", () => {
  const { result } = renderHook(() => useRoomCommandResults("road", false));
  expect(result.current).toEqual([]);
});

it("ranks a matching room first, runs it via openMessage + activateMessagesPanel, and labels a matching person", () => {
  const openMessage = vi.fn();
  const onSelectParticipant = vi.fn();
  vi.mocked(useMessagingWorkspaceOptional).mockReturnValue({
    directoryProps: {
      rooms: [
        { roomId: "room-roadmap", kind: "channel", name: "roadmap", participants: [] },
        { roomId: "room-general", kind: "channel", name: "general", participants: [] },
      ],
      participants: [{ kind: "agent", id: "agent-friday", displayName: "Friday", mentionHandle: null }],
      onSelectParticipant,
      // Unused fields for this hook, present only to satisfy the shape.
    } as never,
    openMessage,
  } as never);

  const { result, rerender } = renderHook(({ query }) => useRoomCommandResults(query, true), {
    initialProps: { query: "road" },
  });

  expect(result.current[0]).toMatchObject({ label: "roadmap", sublabel: "Project" });
  result.current[0].run();
  expect(openMessage).toHaveBeenCalledWith("room-roadmap", null);
  expect(activateMessagesPanel).toHaveBeenCalled();

  rerender({ query: "fri" });
  expect(result.current).toContainEqual(expect.objectContaining({ label: "Message Friday", sublabel: "Agent" }));
});
