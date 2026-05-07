import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Inbox } from "lucide-react";
import { PanelEmptyState } from "@/components/teams/shell/PanelEmptyState";

describe("PanelEmptyState", () => {
  it("renders headline + supporting copy", () => {
    render(
      <PanelEmptyState
        icon={Inbox}
        headline="Inbox zero."
        supporting="Nothing pending right now."
      />
    );
    expect(screen.getByText("Inbox zero.")).toBeInTheDocument();
    expect(screen.getByText("Nothing pending right now.")).toBeInTheDocument();
  });

  it("does not render a CTA when no action prop", () => {
    render(<PanelEmptyState icon={Inbox} headline="x" supporting="y" />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders + fires the CTA when provided", async () => {
    const onAction = vi.fn();
    render(
      <PanelEmptyState
        icon={Inbox}
        headline="x"
        supporting="y"
        action={{ label: "Create routine", onClick: onAction }}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "Create routine" }));
    expect(onAction).toHaveBeenCalledOnce();
  });

  it("renders the icon", () => {
    render(
      <PanelEmptyState icon={Inbox} headline="x" supporting="y" />
    );
    // The icon is decorative (aria-hidden), but we can verify presence via SVG
    const { container } = render(
      <PanelEmptyState icon={Inbox} headline="x" supporting="y" />
    );
    expect(container.querySelector("svg")).toBeInTheDocument();
  });

  it("merges caller className on the root", () => {
    const { container } = render(
      <PanelEmptyState icon={Inbox} headline="x" supporting="y" className="custom" />
    );
    expect(container.firstChild).toHaveClass("custom");
  });
});
