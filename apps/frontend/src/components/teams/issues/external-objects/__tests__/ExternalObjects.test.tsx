import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ExternalObjectPill, type ExternalObjectPillData } from "../ExternalObjectPill";
import { ExternalObjectRows } from "../external-object-rows";
import { ExternalObjectStatusSummary } from "../ExternalObjectStatusSummary";

const PR: ExternalObjectPillData = {
  providerKey: "github",
  objectType: "pull_request",
  iconKey: "github",
  statusCategory: "succeeded",
  liveness: "fresh",
  displayTitle: "Fix the inbox",
  statusLabel: "Merged",
  statusIconKey: "git-merge",
  url: "https://github.com/acme/app/pull/42",
};

describe("ExternalObjectPill", () => {
  it("renders a merged PR as a link, in the merged colour rather than the succeeded one", () => {
    render(<ExternalObjectPill object={PR} />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", PR.url);
    expect(link).toHaveTextContent("PR 42 - Merged");
    expect(link).toHaveStyle({ color: "var(--fg)" });
    expect(screen.getByRole("img", { name: "GitHub: Merged" })).toBeInTheDocument();
  });

  it("renders an auth-required object dashed, with the liveness in its label", () => {
    render(
      <ExternalObjectPill
        object={{ ...PR, statusCategory: "auth_required", liveness: "auth_required", statusLabel: null, statusIconKey: null }}
      />,
    );
    const link = screen.getByRole("link");
    expect(link).toHaveTextContent("PR 42 - Authorization required");
    expect(link.className).toContain("[border-style:dashed]");
    expect(link).toHaveStyle({ color: "var(--warn)" });
    expect(link).toHaveAttribute("aria-label", expect.stringContaining("(Requires auth)"));
  });
});

describe("ExternalObjectRows", () => {
  it("renders nothing for an empty list (the feature is off, or the task mentions nothing)", () => {
    const { container } = render(<dl><ExternalObjectRows externalObjects={[]} /></dl>);
    expect(container.querySelector("dl")).toBeEmptyDOMElement();
  });
});

describe("ExternalObjectStatusSummary", () => {
  it("renders nothing when a task has no external objects", () => {
    const { container } = render(
      <ExternalObjectStatusSummary
        summary={{
          total: 0, byStatusCategory: {}, byLiveness: {}, highestSeverity: "muted",
          staleCount: 0, authRequiredCount: 0, unreachableCount: 0, objects: [],
        }}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
