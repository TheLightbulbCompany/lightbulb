import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { ManifestSummary } from "../page";
import type { ListingManifest, RequirementRow } from "@/lib/marketplace/types";

// page.tsx is an async server component whose default export pulls in the
// chat-stack-heavy BuyButton + markdown renderer. We only render the small
// `ManifestSummary` named export, so stub those module-level imports to keep
// the test fast and free of Clerk/markdown deps. AgentIdentity + agentDesign
// stay real so AgentPill renders actual text.
vi.mock("@/lib/marketplace/api", () => ({
  getListing: vi.fn(),
}));
vi.mock("@/components/marketplace/storefront/Listing/BuyButton", () => ({
  BuyButton: () => null,
}));
vi.mock("@/components/marketplace/storefront/Listing/MarkdownDescription", () => ({
  MarkdownDescription: () => null,
}));
vi.mock("@/components/marketplace/storefront/Listing/OwnerManageLink", () => ({
  OwnerManageLink: () => null,
}));

// Fully-typed mock builders so `tsc --noEmit` (which CI runs over test files)
// stays green.
function row(overrides: Partial<RequirementRow> & { key: string }): RequirementRow {
  return {
    display_name: overrides.key,
    kind: "skill",
    disposition: "included",
    env: [],
    detected_via: [],
    bundled: false,
    required: true,
    ...overrides,
  };
}

function manifest(overrides: Partial<ListingManifest> = {}): ListingManifest {
  return { name: "Test Agent", ...overrides };
}

describe("<ManifestSummary>", () => {
  it("renders a byo requirement with its display name and env var", () => {
    render(
      <ManifestSummary
        manifest={manifest({
          requirements: [
            row({
              key: "parallel",
              display_name: "Parallel",
              kind: "external_api",
              disposition: "byo",
              env: ["PARALLEL_API_KEY"],
            }),
          ],
        })}
      />,
    );
    expect(screen.getByText("Bring your own key")).toBeInTheDocument();
    expect(screen.getByText("Parallel · PARALLEL_API_KEY")).toBeInTheDocument();
  });

  it("groups requirements by disposition into labeled sections", () => {
    render(
      <ManifestSummary
        manifest={manifest({
          requirements: [
            row({ key: "web_search", display_name: "Web Search", disposition: "included" }),
            row({
              key: "slack",
              display_name: "Slack",
              kind: "channel",
              disposition: "connect",
            }),
            row({
              key: "exotic",
              display_name: "Exotic API",
              disposition: "unavailable",
            }),
          ],
        })}
      />,
    );
    expect(screen.getByText("Included")).toBeInTheDocument();
    expect(screen.getByText("Web Search")).toBeInTheDocument();
    expect(screen.getByText("Connect an integration")).toBeInTheDocument();
    expect(screen.getByText("Slack")).toBeInTheDocument();
    expect(screen.getByText("Not available on Lightbulb")).toBeInTheDocument();
    expect(screen.getByText("Exotic API")).toBeInTheDocument();
    // A disposition with no rows must not render a group.
    expect(screen.queryByText("Bring your own key")).toBeNull();
  });

  it("falls back to the requirement key when display_name is empty", () => {
    render(
      <ManifestSummary
        manifest={manifest({
          requirements: [row({ key: "raw_key", display_name: "", disposition: "included" })],
        })}
      />,
    );
    expect(screen.getByText("raw_key")).toBeInTheDocument();
  });

  it("renders the self-contained line when requirements is empty", () => {
    render(<ManifestSummary manifest={manifest({ requirements: [] })} />);
    expect(
      screen.getByText(/Self-contained — no external integrations required\./),
    ).toBeInTheDocument();
  });

  it("renders the self-contained line when requirements is undefined", () => {
    render(<ManifestSummary manifest={manifest()} />);
    expect(
      screen.getByText(/Self-contained — no external integrations required\./),
    ).toBeInTheDocument();
  });

  it("does not render the removed dead-key sections or legacy helper text", () => {
    render(<ManifestSummary manifest={manifest({ requirements: [] })} />);
    expect(screen.queryByText("Required skills")).toBeNull();
    expect(screen.queryByText("Required plugins")).toBeNull();
    expect(screen.queryByText("Required tools")).toBeNull();
    expect(screen.queryByText("Suggested channels")).toBeNull();
    expect(screen.queryByText("None")).toBeNull();
    expect(screen.queryByText(/may need a manual entry/)).toBeNull();
  });

  it("shows the scan-incomplete note only when scan_incomplete is true", () => {
    const { rerender } = render(
      <ManifestSummary
        manifest={manifest({
          requirements: [row({ key: "web_search", disposition: "included" })],
          scan_incomplete: true,
        })}
      />,
    );
    expect(
      screen.getByText("Some dependencies may not have been fully detected."),
    ).toBeInTheDocument();

    rerender(
      <ManifestSummary
        manifest={manifest({
          requirements: [row({ key: "web_search", disposition: "included" })],
          scan_incomplete: false,
        })}
      />,
    );
    expect(
      screen.queryByText("Some dependencies may not have been fully detected."),
    ).toBeNull();
  });

  it("renders the vibe and suggested model fields when present", () => {
    render(
      <ManifestSummary
        manifest={manifest({
          vibe: "Calm and precise",
          suggested_model: "claude-opus",
          requirements: [],
        })}
      />,
    );
    expect(screen.getByText(/Calm and precise/)).toBeInTheDocument();
    expect(screen.getByText("claude-opus")).toBeInTheDocument();
  });
});
