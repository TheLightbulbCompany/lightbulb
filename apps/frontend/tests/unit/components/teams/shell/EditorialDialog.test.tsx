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
});
