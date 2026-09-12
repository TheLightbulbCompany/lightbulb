// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { TooltipProvider } from "@/components/ui/tooltip";
import { FileTree } from "@/components/chat/FileTree";
import type { FileEntry } from "@/hooks/useWorkspaceFiles";

const dir = (path: string): FileEntry => ({ name: path.split("/").pop()!, path, type: "dir", size: null, modified_at: 0 });
const file = (path: string): FileEntry => ({ name: path.split("/").pop()!, path, type: "file", size: 1, modified_at: 0 });

describe("FileTree folders load one level at a time", () => {
  it("fetches a folder's children only when it is expanded, once", async () => {
    const loadChildren = vi.fn(async (p: string) =>
      p === "docs" ? [dir("docs/api"), file("docs/readme.md")] : [file("docs/api/index.md")],
    );
    render(
      <TooltipProvider>
        <FileTree
          files={[dir("docs"), file("plan.md")]}
          selectedPath={null}
          onSelect={() => {}}
          onRefresh={() => {}}
          isLoading={false}
          loadChildren={loadChildren}
        />
      </TooltipProvider>,
    );
    expect(screen.getByText("plan.md")).toBeTruthy();
    expect(loadChildren).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("docs"));
    await waitFor(() => expect(screen.getByText("readme.md")).toBeTruthy());
    expect(loadChildren).toHaveBeenCalledWith("docs");

    fireEvent.click(screen.getByText("api"));
    await waitFor(() => expect(screen.getByText("index.md")).toBeTruthy());
    expect(loadChildren).toHaveBeenCalledWith("docs/api");

    // collapse + re-expand reuses what was loaded
    fireEvent.click(screen.getByText("docs"));
    fireEvent.click(screen.getByText("docs"));
    expect(loadChildren).toHaveBeenCalledTimes(2);
  });
});
