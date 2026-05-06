import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { GalleryItemRow } from "@/components/chat/GalleryItemRow";

const baseAgent = {
  slug: "pitch",
  name: "Pitch",
  version: 3,
  emoji: "🎯",
  vibe: "Direct",
  description: "Sales",
  suggested_model: "qwen",
  suggested_channels: [],
  required_skills: [],
  required_plugins: [],
};

describe("GalleryItemRow", () => {
  it("renders name and monogram", () => {
    render(<GalleryItemRow agent={baseAgent} onDeploy={vi.fn()} onOpenInfo={vi.fn()} />);
    expect(screen.getByText("Pitch")).toBeInTheDocument();
    expect(screen.getByText("Pi")).toBeInTheDocument();
  });

  it("calls onDeploy when + clicked", async () => {
    const onDeploy = vi.fn().mockResolvedValue(undefined);
    render(<GalleryItemRow agent={baseAgent} onDeploy={onDeploy} onOpenInfo={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /deploy/i }));
    expect(onDeploy).toHaveBeenCalledWith("pitch");
  });

  it("calls onOpenInfo when i clicked", async () => {
    const onOpenInfo = vi.fn();
    render(<GalleryItemRow agent={baseAgent} onDeploy={vi.fn()} onOpenInfo={onOpenInfo} />);
    await userEvent.click(screen.getByRole("button", { name: /info/i }));
    expect(onOpenInfo).toHaveBeenCalledWith(baseAgent);
  });

  it("disables deploy button while in-flight", async () => {
    let resolve!: () => void;
    const onDeploy = vi.fn(() => new Promise<void>((r) => { resolve = r; }));
    render(<GalleryItemRow agent={baseAgent} onDeploy={onDeploy} onOpenInfo={vi.fn()} />);
    const btn = screen.getByRole("button", { name: /deploy/i });
    userEvent.click(btn);
    await vi.waitFor(() => expect(btn).toBeDisabled());
    resolve();
  });
});
