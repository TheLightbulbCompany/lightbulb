// @vitest-environment jsdom
/**
 * A rewind hands the removed user message's text back as `editorText`; the
 * composer has to receive it and take focus so the owner can edit and resend
 * without retyping.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { ChatInput } from "../ChatInput";

vi.mock("@/hooks/useSlashCommands", () => ({
  useSlashCommands: (): { commands: never[]; isLoading: boolean } => ({
    commands: [],
    isLoading: false,
  }),
}));

const onSend = (_message: string, _files?: File[]): void => {};
const textarea = () => screen.getByRole("textbox") as HTMLTextAreaElement;

describe("ChatInput — parent-supplied draft", () => {
  it("loads the draft into the composer and focuses it", () => {
    render(<ChatInput onSend={onSend} draft={{ text: "the rewound prompt", nonce: 1 }} />);
    expect(textarea().value).toBe("the rewound prompt");
    expect(document.activeElement).toBe(textarea());
  });

  it("ignores a draft whose nonce has not moved, so it never fights the typist", () => {
    const draft = { text: "restored", nonce: 1 };
    const { rerender } = render(<ChatInput onSend={onSend} draft={draft} />);
    fireEvent.change(textarea(), { target: { value: "what I typed instead" } });

    // A fresh object with the SAME nonce — a re-render, not a new request.
    rerender(<ChatInput onSend={onSend} draft={{ ...draft }} />);
    expect(textarea().value).toBe("what I typed instead");
  });

  it("loads the same text again on a new nonce", () => {
    const { rerender } = render(<ChatInput onSend={onSend} draft={{ text: "again", nonce: 1 }} />);
    fireEvent.change(textarea(), { target: { value: "" } });

    rerender(<ChatInput onSend={onSend} draft={{ text: "again", nonce: 2 }} />);
    expect(textarea().value).toBe("again");
  });

  it("a composer that mounts AFTER the request still loads it — the remount a rewind causes", () => {
    const draft = { text: "survives the remount", nonce: 4 };
    const { unmount } = render(<ChatInput onSend={onSend} draft={draft} />);
    unmount();

    render(<ChatInput onSend={onSend} draft={draft} />);
    expect(textarea().value).toBe("survives the remount");
  });

  it("an empty editorText clears the composer rather than leaving a stale draft", () => {
    const { rerender } = render(<ChatInput onSend={onSend} draft={{ text: "first", nonce: 1 }} />);
    rerender(<ChatInput onSend={onSend} draft={{ text: "", nonce: 2 }} />);
    expect(textarea().value).toBe("");
  });
});
