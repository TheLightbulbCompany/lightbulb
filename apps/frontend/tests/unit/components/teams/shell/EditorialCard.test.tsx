import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { EditorialCard } from "@/components/teams/shell/EditorialCard";

describe("EditorialCard", () => {
  it("renders children inside the editorial-card class", () => {
    render(<EditorialCard><span>inside</span></EditorialCard>);
    const child = screen.getByText("inside");
    expect(child.parentElement).toHaveClass("editorial-card");
  });

  it("adds editorial-card-hover when hover prop set", () => {
    render(<EditorialCard hover><span>inside</span></EditorialCard>);
    expect(screen.getByText("inside").parentElement).toHaveClass("editorial-card-hover");
  });

  it("forwards className", () => {
    render(<EditorialCard className="custom"><span>inside</span></EditorialCard>);
    expect(screen.getByText("inside").parentElement).toHaveClass("custom");
  });

  it("does not add hover class by default", () => {
    render(<EditorialCard><span>inside</span></EditorialCard>);
    expect(screen.getByText("inside").parentElement).not.toHaveClass("editorial-card-hover");
  });
});
