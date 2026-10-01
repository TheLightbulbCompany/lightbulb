import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { LocalSession } from "@lightbulb/shared";
import { SessionRenameButton } from "../SessionRenameButton";

afterEach(cleanup);

const session = { sessionId: "s1", provider: "codex", title: "Original", displayTitle: "Chosen" } as LocalSession;

it("renames by stable session ID and can restore the provider title", async () => {
  const rename = vi.fn().mockResolvedValue({});
  render(<SessionRenameButton session={session} onRename={rename} />);
  fireEvent.click(screen.getByRole("button", { name: "Rename Chosen" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Session name" }), { target: { value: "  Better name  " } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(rename).toHaveBeenCalledWith("s1", "Better name"));
  fireEvent.click(screen.getByRole("button", { name: "Rename Chosen" }));
  fireEvent.click(screen.getByRole("button", { name: "Use original title" }));
  await waitFor(() => expect(rename).toHaveBeenLastCalledWith("s1", null));
});
