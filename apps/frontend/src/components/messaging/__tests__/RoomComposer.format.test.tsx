import { useState } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomComposer } from "../RoomComposer";
import { composerBox, composerValue, selectInComposer, typeInComposer, typeKeys } from "@/test-utils/composer";
import { editorOf } from "../ComposerEditor";
import { lexical } from "../lexical";
import type { RoomParticipant } from "../types";
import { MarkdownView } from "@/components/voice/MarkdownView";

function Harness({ members = [] }: { members?: readonly RoomParticipant[] }) {
  const [text, setText] = useState("ship it");
  return <RoomComposer text={text} onTextChange={setText} members={members}
    clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={vi.fn()} />;
}

const field = () => composerBox();

beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("founder 6 Nov: typed characters go out escaped so they show as typed; code and URLs are untouched", () => {
  const onSubmit = vi.fn();
  function Send() {
    const [text, setText] = useState("");
    return <RoomComposer text={text} onTextChange={setText} members={[]}
      clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={onSubmit} />;
  }
  render(<Send />);
  const cases: [string, string][] = [
    ["__init__", "\\_\\_init\\_\\_"], ["3*4 then 5*6", "3\\*4 then 5\\*6"], ["~x~", "\\~x\\~"], ["a_b_c", "a_b_c"],
    ["**not bold**", "\\*\\*not bold\\*\\*"], ["# not a heading", "\\# not a heading"],
    ["see https://x.com/a_b~c, @growth_lead", "see https://x.com/a_b~c, @growth_lead"],
  ];
  for (const [typed, stored] of cases) {
    typeInComposer("");
    typeKeys(typed);
    expect(field().querySelector("strong, em, s, .font-semibold, .italic, .line-through, a")).toBeNull();
    expect(composerValue()).toBe(stored);
    // Read back (a draft, an edit): still the literal text, nothing formatted.
    typeInComposer(stored);
    expect(field()).toHaveTextContent(typed);
    expect(field().querySelector("strong, em, s, .font-semibold, .italic, .line-through")).toBeNull();
    expect(composerValue()).toBe(stored);
    // And the sent message shows exactly what was typed.
    const { container, unmount } = render(<MarkdownView skipHtml>{stored}</MarkdownView>);
    expect(container).toHaveTextContent(typed);
    expect(container.querySelector("strong, em, del, h1")).toBeNull();
    unmount();
  }
  // Inside a real code block nothing is escaped.
  typeInComposer("```\na*b_c\n```");
  expect(field().querySelector("code")).not.toBeNull();
  expect(composerValue()).toBe("```\na*b_c\n```");
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ text: "```\na*b_c\n```" }));
});

it("founder 6 Nov: bold and italic in a draft read back shown formatted, not as **", () => {
  const saved = "**ship** *it*";
  render(<RoomComposer text={saved} onTextChange={vi.fn()} members={[]}
    clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={vi.fn()} />);
  expect(field().querySelector(".font-semibold")).toHaveTextContent("ship");
  expect(field().querySelector(".italic")).toHaveTextContent("it");
  expect(field()).not.toHaveTextContent("*");
  expect(composerValue()).toBe(saved);
});

it("review: an empty bullet, quote or code block is nothing to send", () => {
  const onSubmit = vi.fn();
  const onEditLast = vi.fn();
  function Send() {
    const [text, setText] = useState("");
    return <RoomComposer text={text} onTextChange={setText} members={[]} onEditLast={onEditLast}
      clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={onSubmit} />;
  }
  render(<Send />);
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  fireEvent.keyDown(field(), { key: "Enter" });
  expect(onSubmit).not.toHaveBeenCalled();
  fireEvent.keyDown(field(), { key: "ArrowUp" });
  expect(onEditLast).toHaveBeenCalledOnce();
  for (const blank of ["> ", "```\n```", "1. "]) {
    typeInComposer(blank);
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  }
});

it("W18 prod block: typing _ * ~ makes no formatting; inside code nothing is transformed or escaped", () => {
  render(<Harness />);
  for (const value of ["__init__", "run __main__ now", "~tilde~", "3*4 then 5*6", "**not bold** _nor this_"]) {
    typeInComposer("");
    typeKeys(value);
    expect(field().querySelector("strong, em, s, .font-semibold, .italic, .line-through")).toBeNull();
    expect(field()).toHaveTextContent(value);
  }
  typeInComposer("```\n```");
  selectInComposer(0, 0);
  typeKeys("def __init__(self):");
  expect(composerValue()).toBe("```\ndef __init__(self):\n```");
});

it("review #4954: inline code keeps its backslashes on read-back; a handle opening with _ stays a handle", () => {
  for (const stored of ["`code \\* x` and `` a\\*b`c ``", "```\nprint('\\*')\n```"]) {
    render(<RoomComposer text={stored} onTextChange={vi.fn()} members={[]}
      clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={vi.fn()} />);
    expect(field()).toHaveTextContent(stored.includes("```") ? "print('\\*')" : "code \\* x");
    // Touched, it writes the same text back.
    selectInComposer(0, 0);
    typeKeys("");
    act(() => editorOf(field()).update(() => { lexical.$getRoot().selectEnd(); }, { discrete: true }));
    expect(composerValue()).toBe(stored);
    cleanup();
  }
  render(<Harness />);
  typeInComposer("");
  typeKeys("ping @_ops now");
  expect(composerValue()).toBe("ping @_ops now");
});
