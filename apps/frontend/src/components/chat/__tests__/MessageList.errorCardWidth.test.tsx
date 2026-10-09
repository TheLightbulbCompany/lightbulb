/**
 * Error bubbles must fill the row instead of shrink-to-fit.
 *
 * Error cards are wider than ordinary assistant bubbles so their recovery
 * copy and action remain readable in the transcript.
 *
 * jsdom computes no layout, so the guard is the class on the wrapper.
 */

import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { MessageList } from "../MessageList";
import type { HistoryMessage, RenderGroup } from "@/hooks/internal/types";

function assistantGroup(message: HistoryMessage): RenderGroup[] {
  return [{ side: "assistant", key: "g1", items: [{ kind: "message", key: "m1", message }] }];
}

function renderAssistant(message: HistoryMessage) {
  return render(
    <MessageList
      renderGroups={assistantGroup(message)}
      isStreaming={false}
      agentName="Friday"
    />,
  );
}

function bubbleFor(container: HTMLElement): Element {
  const row = container.querySelector('[data-role="assistant"]');
  expect(row).not.toBeNull();
  return row!.firstElementChild!;
}

describe("MessageList — error bubbles fill the row", () => {
  it("adds w-full to a bubble whose message carries an errorCode", () => {
    const { container } = renderAssistant({
      role: "assistant",
      content: "Error: upstream timed out",
      errorCode: "run_error",
    });
    expect(bubbleFor(container).classList.contains("w-full")).toBe(true);
  });

  it("keeps plain bubbles shrink-to-fit", () => {
    const { container } = renderAssistant({ role: "assistant", content: "hello" });
    expect(bubbleFor(container).classList.contains("w-full")).toBe(false);
  });
});
