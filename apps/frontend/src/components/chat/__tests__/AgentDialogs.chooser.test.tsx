// The shared AgentCreateDialog (desktop WorkspaceSidebar "+" and mobile
// ChatLayout) opens on a chooser step steering toward the marketplace;
// the blank-agent form is behind "Build your own".
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const mockOpenMarketplace = vi.fn();
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ open: mockOpenMarketplace }),
}));

import { AgentCreateDialog } from "@/components/chat/AgentDialogs";

describe("AgentCreateDialog chooser step", () => {
  it("hire path closes the dialog and opens the marketplace drawer", () => {
    mockOpenMarketplace.mockClear();
    const onCancel = vi.fn();
    render(
      <AgentCreateDialog
        open
        existingNames={[]}
        onCancel={onCancel}
        onCreate={vi.fn()}
      />,
    );

    // Chooser renders first — no name input yet.
    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /hire from marketplace/i }));
    expect(onCancel).toHaveBeenCalled();
    expect(mockOpenMarketplace).toHaveBeenCalledWith({ view: "browse" });
  });

  it("build path reveals the name form and submits through onCreate", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    render(
      <AgentCreateDialog
        open
        existingNames={[]}
        onCancel={vi.fn()}
        onCreate={onCreate}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /build your own/i }));
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Helper" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));
    await vi.waitFor(() => expect(onCreate).toHaveBeenCalledWith("Helper"));
  });
});
