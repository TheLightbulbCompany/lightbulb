/**
 * Fenced code blocks run through the PrismLight build with an explicit set of
 * registered grammars (the barrel export bundles all ~300 refractor grammars).
 * Three things must hold: registered languages — including their aliases and
 * whatever case the model emitted — still highlight, and a fence naming
 * anything else degrades to plain text instead of reaching refractor, which
 * throws on an unregistered grammar. All are asserted through the rendered
 * output — `resolveHighlightLanguage` is module-private.
 */

import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { MessageList } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

function fenceGroups(language: string, code: string): RenderGroup[] {
  return [
    {
      side: "assistant",
      key: "g1",
      items: [
        {
          kind: "message",
          key: "g1:m",
          message: {
            role: "assistant",
            content: [{ type: "text", text: "```" + language + "\n" + code + "\n```" }],
          },
        },
      ],
    },
  ];
}

function renderFence(language: string, code: string) {
  return render(
    <MessageList
      renderGroups={fenceGroups(language, code)}
      isStreaming={false}
      agentName="Friday"
    />,
  );
}

describe("MessageList code fences", () => {
  it("highlights a registered language", () => {
    const { container } = renderFence("python", "def hello():\n    return 1");

    expect(container.querySelectorAll("span.token").length).toBeGreaterThan(0);
    expect(container.textContent).toContain("def hello():");
  });

  it("highlights via a grammar's alias", () => {
    const { container } = renderFence("py", "import os");

    expect(container.querySelectorAll("span.token").length).toBeGreaterThan(0);
  });

  it("highlights a registered language the fence spelled in another case", () => {
    // The grammar set is keyed lowercase, so resolveHighlightLanguage
    // normalizes before the lookup. Without that, "Python" misses and falls
    // back to plain text — the same outcome as an unregistered grammar
    // below, which is why this asserts tokens rather than absence of throw.
    const { container } = renderFence("Python", "def hello():\n    return 1");

    expect(container.querySelectorAll("span.token").length).toBeGreaterThan(0);
    expect(container.textContent).toContain("def hello():");
  });

  it("renders an unregistered language as plain text without throwing", () => {
    const { container } = renderFence("brainfuck", "+++[->+++<]");

    expect(container.querySelectorAll("span.token").length).toBe(0);
    expect(container.textContent).toContain("+++[->+++<]");
  });

  it("labels the block with the language the fence declared", () => {
    const { container } = renderFence("brainfuck", "+++");

    expect(container.textContent).toContain("brainfuck");
  });
});
