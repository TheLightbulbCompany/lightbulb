/**
 * Fenced code blocks run through the PrismLight build with an explicit set of
 * registered grammars (the barrel export bundles all ~300 refractor grammars).
 * Two things must hold: registered languages — including their aliases — still
 * highlight, and a fence naming anything else degrades to plain text instead
 * of reaching refractor, which throws on an unregistered grammar.
 */

import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { MessageList, resolveHighlightLanguage } from "../MessageList";
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
      isTyping={false}
      agentName="Friday"
    />,
  );
}

describe("resolveHighlightLanguage", () => {
  it("passes through registered grammars", () => {
    for (const language of ["python", "typescript", "bash", "json", "yaml", "markdown", "sql", "docker", "diff", "rust"]) {
      expect(resolveHighlightLanguage(language)).toBe(language);
    }
  });

  it("resolves the aliases those grammars register", () => {
    expect(resolveHighlightLanguage("py")).toBe("py");
    expect(resolveHighlightLanguage("sh")).toBe("sh");
    expect(resolveHighlightLanguage("shell")).toBe("shell");
    expect(resolveHighlightLanguage("ts")).toBe("ts");
    expect(resolveHighlightLanguage("yml")).toBe("yml");
    expect(resolveHighlightLanguage("md")).toBe("md");
    expect(resolveHighlightLanguage("dockerfile")).toBe("dockerfile");
    expect(resolveHighlightLanguage("html")).toBe("html");
  });

  it("normalizes case", () => {
    expect(resolveHighlightLanguage("Python")).toBe("python");
    expect(resolveHighlightLanguage("JSON")).toBe("json");
  });

  it("falls back to plain text for unregistered grammars", () => {
    expect(resolveHighlightLanguage("brainfuck")).toBe("text");
    expect(resolveHighlightLanguage("cobol")).toBe("text");
    expect(resolveHighlightLanguage("")).toBe("text");
  });
});

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
