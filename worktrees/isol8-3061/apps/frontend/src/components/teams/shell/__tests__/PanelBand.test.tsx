import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PanelBand, PanelBandEmpty } from "@/components/teams/shell";

describe("PanelBand", () => {
  it("uses the id verbatim so a section deep link can scroll to it", () => {
    const { container } = render(
      <PanelBand id="inbox-needs-you" label="Needs you">
        <p>body</p>
      </PanelBand>,
    );
    expect(container.querySelector("#inbox-needs-you")).not.toBeNull();
  });

  it("labels the section for assistive tech and renders the right slot", () => {
    render(
      <PanelBand id="money-payments" label="Payments" right={<span>3 shown</span>}>
        <p>body</p>
      </PanelBand>,
    );
    expect(screen.getByRole("region", { name: "Payments" })).toBeInTheDocument();
    expect(screen.getByText("3 shown")).toBeInTheDocument();
  });

  it("announces an empty line as a status and an error as an alert", () => {
    const { rerender } = render(<PanelBandEmpty>Nothing yet.</PanelBandEmpty>);
    expect(screen.getByRole("status")).toHaveTextContent("Nothing yet.");
    rerender(<PanelBandEmpty role="alert">Could not load.</PanelBandEmpty>);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load.");
  });
});
