import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { CaseFieldValue } from "@/components/teams/cases/CaseFieldsPanel";

describe("CaseFieldValue", () => {
  it("renders an explicit false as its own mark, not the missing-value dash", () => {
    const { rerender } = render(<CaseFieldValue value={false} />);
    expect(screen.getByLabelText("false")).toBeInTheDocument();
    expect(screen.queryByText("—")).toBeNull();

    rerender(<CaseFieldValue value />);
    expect(screen.getByLabelText("true")).toBeInTheDocument();

    for (const missing of [null, undefined, ""]) {
      rerender(<CaseFieldValue value={missing} />);
      expect(screen.getByText("—")).toBeInTheDocument();
    }
  });
});
