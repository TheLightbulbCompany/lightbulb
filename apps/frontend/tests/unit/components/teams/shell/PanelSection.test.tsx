import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PanelSection } from "@/components/teams/shell/PanelSection";

describe("PanelSection", () => {
  it("renders label + count + children when open", () => {
    render(
      <PanelSection label="Today" count={2} open onOpenChange={() => {}}>
        <div>row</div>
      </PanelSection>
    );
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("row")).toBeInTheDocument();
  });

  it("hides children when closed", () => {
    render(
      <PanelSection label="X" open={false} onOpenChange={() => {}}>
        <div data-testid="row-content">row</div>
      </PanelSection>
    );
    // shadcn Collapsible uses Radix which removes content from the DOM when closed (or hides via CSS)
    // Either way, the data-testid should not be visible to user-event
    const content = screen.queryByTestId("row-content");
    if (content) {
      expect(content).not.toBeVisible();
    } else {
      expect(content).toBeNull();
    }
  });

  it("toggles via the chevron trigger", async () => {
    const onOpenChange = vi.fn();
    render(
      <PanelSection label="X" open onOpenChange={onOpenChange}>
        <div>row</div>
      </PanelSection>
    );
    await userEvent.click(screen.getByRole("button", { name: /X/i }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("does not render count badge when count is undefined", () => {
    render(
      <PanelSection label="No Count" open onOpenChange={() => {}}>
        <div>x</div>
      </PanelSection>
    );
    // The label "No Count" is present; no numeric count rendered alongside it.
    // The trigger should contain exactly the label, no count text node.
    const trigger = screen.getByRole("button", { name: /No Count/i });
    expect(trigger).toBeInTheDocument();
    // Verify no count number is in the trigger's text content
    expect(trigger.textContent).toBe("No Count");
  });

  it("renders count badge when count is 0", () => {
    // 0 is a valid count (e.g., "Today: 0 items"); should render
    render(
      <PanelSection label="X" count={0} open onOpenChange={() => {}}>
        <div>row</div>
      </PanelSection>
    );
    const trigger = screen.getByRole("button", { name: /X/i });
    expect(trigger.textContent).toContain("0");
  });
});
