import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { Identity } from "../Identity";

describe("Identity avatarOnly", () => {
  it("renders the name as text by default", () => {
    render(<Identity name="Friday" />);
    expect(screen.getByText("Friday")).toBeInTheDocument();
  });

  it("hides the name text and exposes it via role=img when avatarOnly", () => {
    render(<Identity name="Friday" avatarOnly />);
    expect(screen.queryByText("Friday")).toBeNull();
    expect(screen.getByRole("img", { name: "Friday" })).toBeInTheDocument();
  });
});
