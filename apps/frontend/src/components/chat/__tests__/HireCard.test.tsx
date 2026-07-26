import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HireCard, parseHireSuggestion } from "../HireCard";

const post = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ post }),
}));

const SUGGESTION = {
  kind: "isol8.hire_suggestion",
  reason: "Nobody on the team does content.",
  listings: [
    {
      slug: "pulse",
      name: "Pulse",
      summary: "Marketing intel and content research.",
      tags: ["marketing", "content"],
      price_cents: 0,
    },
  ],
  approval_id: null,
};

beforeEach(() => {
  post.mockReset();
  post.mockResolvedValue({});
});

describe("parseHireSuggestion", () => {
  it("accepts a well-formed suggestion", () => {
    expect(parseHireSuggestion(SUGGESTION)?.listings).toHaveLength(1);
  });

  it("rejects anything else, so ordinary tools fall through to the pill", () => {
    // The value crosses a process boundary from the container — nothing about
    // its shape may be assumed.
    expect(parseHireSuggestion({ kind: "something.else" })).toBeNull();
    expect(parseHireSuggestion(null)).toBeNull();
    expect(parseHireSuggestion(undefined)).toBeNull();
    expect(parseHireSuggestion("a string")).toBeNull();
    expect(parseHireSuggestion(42)).toBeNull();
    expect(parseHireSuggestion({ kind: "isol8.hire_suggestion" })).toBeNull();
  });

  it("rejects a suggestion whose listings are all malformed", () => {
    expect(
      parseHireSuggestion({ kind: "isol8.hire_suggestion", listings: [{ no: "slug" }] }),
    ).toBeNull();
  });

  it("tolerates a missing reason", () => {
    const parsed = parseHireSuggestion({ ...SUGGESTION, reason: undefined });
    expect(parsed?.reason).toBe("");
  });
});

describe("HireCard", () => {
  it("renders the listing with a deploy affordance", () => {
    render(<HireCard suggestion={SUGGESTION} />);
    expect(screen.getByText("Pulse")).toBeInTheDocument();
    expect(screen.getByText("Marketing intel and content research.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /deploy/i })).toBeInTheDocument();
  });

  it("labels a zero-price listing as free", () => {
    render(<HireCard suggestion={SUGGESTION} />);
    expect(screen.getByText(/free/i)).toBeInTheDocument();
  });

  it("shows a real price for a paid listing", () => {
    render(
      <HireCard
        suggestion={{
          ...SUGGESTION,
          listings: [{ ...SUGGESTION.listings[0], price_cents: 2500 }],
        }}
      />,
    );
    expect(screen.getByText("$25.00")).toBeInTheDocument();
  });

  it("deploys through the existing authenticated route on click", async () => {
    const user = userEvent.setup();
    render(<HireCard suggestion={SUGGESTION} />);
    await user.click(screen.getByRole("button", { name: /deploy/i }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/marketplace/listings/pulse/deploy", {}),
    );
  });

  it("confirms in place once deployed, and does not offer to deploy twice", async () => {
    const user = userEvent.setup();
    render(<HireCard suggestion={SUGGESTION} />);
    await user.click(screen.getByRole("button", { name: /deploy/i }));
    await waitFor(() => expect(screen.getByRole("button")).toBeDisabled());
    expect(screen.getByRole("button").textContent).toMatch(/added/i);
  });

  it("surfaces a failure instead of silently doing nothing", async () => {
    post.mockRejectedValue(new Error("listing is no longer available"));
    const user = userEvent.setup();
    render(<HireCard suggestion={SUGGESTION} />);
    await user.click(screen.getByRole("button", { name: /deploy/i }));
    await waitFor(() =>
      expect(screen.getByText(/listing is no longer available/i)).toBeInTheDocument(),
    );
    // Recoverable: the button goes back to offering the deploy.
    expect(screen.getByRole("button")).not.toBeDisabled();
  });

  it("renders one row per recommended listing", () => {
    render(
      <HireCard
        suggestion={{
          ...SUGGESTION,
          listings: [
            SUGGESTION.listings[0],
            { ...SUGGESTION.listings[0], slug: "ora", name: "Ora" },
          ],
        }}
      />,
    );
    expect(screen.getAllByRole("button", { name: /deploy/i })).toHaveLength(2);
  });
});
