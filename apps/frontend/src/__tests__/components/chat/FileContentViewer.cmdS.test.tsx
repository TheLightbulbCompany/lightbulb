// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";

vi.mock("@/components/chat/LobsterGraphView", () => ({
  LobsterGraphView: () => <div>graph</div>,
}));

import { FileContentViewer } from "@/components/chat/FileContentViewer";
import type { FileInfo } from "@/hooks/useWorkspaceFiles";

function fileOf(name: string, content: string): FileInfo {
  return {
    name,
    path: "/" + name,
    size: content.length,
    modified_at: 0,
    content,
    binary: false,
    mime_type: "text/plain",
  };
}

function pressCmdS() {
  fireEvent.keyDown(window, { key: "s", metaKey: true });
}

// Flush the async save promise inside act() so state updates settle cleanly.
async function flush() {
  await act(async () => {});
}

describe("FileContentViewer Cmd+S scoping", () => {
  it("saves when focus is inside the viewer", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const { container } = render(
      <FileContentViewer file={fileOf("notes.md", "hello")} onSave={onSave} isLoading={false} error={null} />,
    );
    const textarea = container.querySelector("textarea")!;
    expect(textarea).toBeTruthy();
    // Dirty the content so canSave/handleSave will actually fire.
    fireEvent.change(textarea, { target: { value: "hello world" } });
    textarea.focus();
    pressCmdS();
    await flush();
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith("hello world");
  });

  it("does NOT save when focus is outside the viewer", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const { container } = render(
      <FileContentViewer file={fileOf("notes.md", "hello")} onSave={onSave} isLoading={false} error={null} />,
    );
    const textarea = container.querySelector("textarea")!;
    // Dirty the content — under the old unscoped handler this would save.
    fireEvent.change(textarea, { target: { value: "hello world" } });
    const outside = document.createElement("input");
    document.body.appendChild(outside);
    outside.focus();
    expect(document.activeElement).toBe(outside);
    pressCmdS();
    await flush();
    expect(onSave).not.toHaveBeenCalled();
    outside.remove();
  });

  it("saves from the Code tab of a .lobster viewer (tabbed branch hosts the ref)", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const { container, getByRole } = render(
      <FileContentViewer file={fileOf("wf.lobster", "steps:\n  - id: a")} onSave={onSave} isLoading={false} error={null} />,
    );
    fireEvent.click(getByRole("tab", { name: /code/i }));
    const textarea = container.querySelector("textarea")!;
    expect(textarea).toBeTruthy();
    fireEvent.change(textarea, { target: { value: "steps:\n  - id: b" } });
    textarea.focus();
    pressCmdS();
    await flush();
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith("steps:\n  - id: b");
  });
});
