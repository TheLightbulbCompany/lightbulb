import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EditorialDialog } from "@/components/teams/shell/EditorialDialog";

describe("EditorialDialog", () => {
  it("does not render content when closed", () => {
    render(
      <EditorialDialog open={false} onOpenChange={() => {}} title="Hello">
        body content
      </EditorialDialog>
    );
    expect(screen.queryByText("body content")).not.toBeInTheDocument();
  });

  it("renders title + content when open", () => {
    render(
      <EditorialDialog open onOpenChange={() => {}} title="Hello">
        body content
      </EditorialDialog>
    );
    expect(screen.getByText("Hello")).toBeInTheDocument();
    expect(screen.getByText("body content")).toBeInTheDocument();
  });

  it("calls onOpenChange when escape pressed", async () => {
    const onOpenChange = vi.fn();
    render(
      <EditorialDialog open onOpenChange={onOpenChange} title="X">
        body
      </EditorialDialog>
    );
    await userEvent.keyboard("{Escape}");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("renders description when provided", () => {
    render(
      <EditorialDialog open onOpenChange={() => {}} title="X" description="more info">
        body
      </EditorialDialog>
    );
    expect(screen.getByText("more info")).toBeInTheDocument();
  });

  it("renders footer when provided", () => {
    render(
      <EditorialDialog open onOpenChange={() => {}} title="X" footer={<button>OK</button>}>
        body
      </EditorialDialog>
    );
    expect(screen.getByRole("button", { name: "OK" })).toBeInTheDocument();
  });

  it("merges caller className onto DialogContent", () => {
    render(
      <EditorialDialog open onOpenChange={() => {}} title="X" className="custom-class">
        body
      </EditorialDialog>
    );
    // DialogContent has role="dialog" — find via that
    expect(screen.getByRole("dialog")).toHaveClass("custom-class");
  });

  it("does not render visible description when omitted", () => {
    render(
      <EditorialDialog open onOpenChange={() => {}} title="X">
        body
      </EditorialDialog>
    );
    // The visible description region should not be present
    // (the sr-only fallback is intentionally invisible — it should not surface to getByText)
    const dialog = screen.getByRole("dialog");
    expect(dialog.querySelector(".text-\\[var\\(--mute\\)\\]")).toBeNull();
  });
});
