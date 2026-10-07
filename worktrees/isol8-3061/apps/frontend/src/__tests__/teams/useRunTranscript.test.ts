// apps/frontend/src/__tests__/teams/useRunTranscript.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { useRunTranscript, RUN_TRANSCRIPT_LIMIT_BYTES } from "@/components/teams/runs/useRunTranscript";
import { resolveMessageBlocks } from "@lightbulb/shared";
import type { HistoryMessage } from "@lightbulb/shared";

beforeEach(() => read.mockReset());

const line = (chunk: string) => JSON.stringify({ ts: "t", stream: "stdout", chunk }) + "\n";

/** Non-event stdout lines fold into one text block; read it back as text. */
const textOf = (messages: HistoryMessage[]) =>
  messages.length > 0 ? (resolveMessageBlocks(messages[0])[0]?.text ?? "") : "";

describe("useRunTranscript", () => {
  it("reads from offset 0 for small logs and reports untruncated", () => {
    read.mockReturnValue({
      data: { content: line("hello") + line("world") },
      isLoading: false,
    });
    const { result } = renderHook(() =>
      useRunTranscript("r1", { active: false, logBytes: 128 }),
    );
    expect(read).toHaveBeenCalledWith(
      `/heartbeat-runs/r1/log?limitBytes=${RUN_TRANSCRIPT_LIMIT_BYTES}`,
      expect.anything(),
    );
    expect(textOf(result.current.messages)).toBe("hello\nworld\n");
    expect(result.current.truncated).toBe(false);
  });

  it("tail-reads oversized logs from logBytes-cap, dropping the partial first record", () => {
    // The tail offset can land mid-record — the first "line" of the read is
    // garbage and must be dropped before parsing.
    read.mockReturnValue({
      data: { content: line("partial-garbage") + line("tail") },
      isLoading: false,
    });
    const logBytes = RUN_TRANSCRIPT_LIMIT_BYTES + 5_000;
    const { result } = renderHook(() =>
      useRunTranscript("r1", { active: false, logBytes }),
    );
    expect(read).toHaveBeenCalledWith(
      `/heartbeat-runs/r1/log?offset=5000&limitBytes=${RUN_TRANSCRIPT_LIMIT_BYTES}`,
      expect.anything(),
    );
    expect(textOf(result.current.messages)).toBe("tail\n");
    expect(result.current.truncated).toBe(true);
  });
});
