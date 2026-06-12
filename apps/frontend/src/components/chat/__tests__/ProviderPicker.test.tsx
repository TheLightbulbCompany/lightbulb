import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

// The ProviderPicker is currently defined as a non-exported function inside
// ProvisioningStepper.tsx. To test it directly we'll export it from that
// file (see Step 3 of this task) and import it here.
import { ProviderPicker } from "../ProvisioningStepper";

// useApi is invoked inside the component on Pick — stub it.
vi.mock("@/lib/api", () => ({
  useApi: () => ({
    post: vi.fn(),
    get: vi.fn(),
  }),
}));

describe("ProviderPicker", () => {
  it("renders all three provider cards for personal users", () => {
    render(<ProviderPicker isOrg={false} />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByText("Bring your own API key")).toBeInTheDocument();
    expect(screen.getByText("Powered by Claude")).toBeInTheDocument();
  });

  it("shows the ChatGPT OAuth card to org users too, with the connector hint", () => {
    // New contract: the org owner's ChatGPT account backs the org container
    // (one account ↔ one container), so org users see all three cards. The
    // org-only hint under the ChatGPT card spells out the connector model.
    render(<ProviderPicker isOrg={true} orgName="Acme" />);
    expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
    expect(screen.getByText("Bring your own API key")).toBeInTheDocument();
    expect(screen.getByText("Powered by Claude")).toBeInTheDocument();
    expect(
      screen.getByText(/You connect your ChatGPT account/i),
    ).toBeInTheDocument();
  });

  it("omits the org-only ChatGPT connector hint for personal users", () => {
    render(<ProviderPicker isOrg={false} />);
    expect(
      screen.queryByText(/You connect your ChatGPT account/i),
    ).not.toBeInTheDocument();
  });

  it('uses the "Three ways" headline for both personal and org users', () => {
    render(<ProviderPicker isOrg={false} />);
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(
      "One price. Three ways to power it.",
    );

    render(<ProviderPicker isOrg={true} orgName="Acme" />);
    const headings = screen.getAllByRole("heading", { level: 2 });
    expect(headings[headings.length - 1]).toHaveTextContent(
      "One price. Three ways to power it.",
    );
  });

  it("uses the 3-col grid for both org and personal users", () => {
    const { container: orgContainer } = render(
      <ProviderPicker isOrg={true} orgName="Acme" />,
    );
    expect(orgContainer.querySelector(".md\\:grid-cols-3")).toBeInTheDocument();

    const { container: personalContainer } = render(<ProviderPicker isOrg={false} />);
    expect(personalContainer.querySelector(".md\\:grid-cols-3")).toBeInTheDocument();
  });
});
