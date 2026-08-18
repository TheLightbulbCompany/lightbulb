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
      listing_id: "81ab74f8-efc2-464c-8c62-706274b69639",
      slug: "pulse",
      name: "Pulse",
      summary: "Marketing intel and content research.",
      tags: ["marketing", "content"],
      price_cents: 0,
    },
  ],
  approval_id: null,
};

function listingAt(n: number) {
  return {
    listing_id: `listing-${n}`,
    slug: `agent-${n}`,
    name: `Agent ${n}`,
    summary: `Does the job of agent ${n}.`,
    tags: [],
    price_cents: 0,
  };
}

const FIVE_LISTINGS = [1, 2, 3, 4, 5].map(listingAt);

// Every mock is (re)armed here, never at module scope — the suite's global
// `afterEach(vi.restoreAllMocks)` strips module-scope `mockResolvedValue`
// calls, which silently broke a previous task's tests in CI.
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

  it("rejects a listing with no listing_id, since deploy cannot address it", () => {
    // Slugs are non-unique; listing_id is the canonical identity. A row
    // without one is unusable, and rendering it would produce a card whose
    // button 404s.
    expect(
      parseHireSuggestion({
        kind: "isol8.hire_suggestion",
        listings: [{ slug: "pulse", name: "Pulse" }],
      }),
    ).toBeNull();
  });

  it("tolerates a missing reason", () => {
    const parsed = parseHireSuggestion({ ...SUGGESTION, reason: undefined });
    expect(parsed?.reason).toBe("");
  });
});

describe("HireCard — single listing", () => {
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
      expect(post).toHaveBeenCalledWith(
        "/marketplace/listings/81ab74f8-efc2-464c-8c62-706274b69639/deploy",
        {},
      ),
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

  it("renders no alternatives drawer when there is nothing to hide", () => {
    // A single-listing suggestion has zero alternatives — the toggle must
    // not render at all, not render disabled or empty.
    render(<HireCard suggestion={SUGGESTION} />);
    expect(screen.queryByRole("button", { name: /alternative/i })).not.toBeInTheDocument();
  });
});

describe("HireCard — multiple listings", () => {
  it("shows one recommendation and hides the rest behind a drawer", async () => {
    render(
      <HireCard
        suggestion={{ kind: "isol8.hire_suggestion", reason: "r", listings: FIVE_LISTINGS }}
      />,
    );
    expect(screen.getAllByRole("article")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: /4 alternatives/i }));
    expect(screen.getAllByRole("article")).toHaveLength(5);
  });

  it("only the primary card carries a Deploy button — collapsing five gates to one is deliberate", async () => {
    render(
      <HireCard
        suggestion={{ kind: "isol8.hire_suggestion", reason: "r", listings: FIVE_LISTINGS }}
      />,
    );
    expect(screen.getAllByRole("button", { name: /deploy/i })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: /4 alternatives/i }));
    // Expanding the drawer must not manifest four more approval gates.
    expect(screen.getAllByRole("button", { name: /deploy/i })).toHaveLength(1);
  });

  it("uses singular wording for exactly one alternative", () => {
    render(
      <HireCard
        suggestion={{
          kind: "isol8.hire_suggestion",
          reason: "r",
          listings: FIVE_LISTINGS.slice(0, 2),
        }}
      />,
    );
    expect(screen.getByRole("button", { name: /^1 alternative$/i })).toBeInTheDocument();
  });

  it("promotes an alternative to primary WITHOUT deploying anything", async () => {
    render(
      <HireCard
        suggestion={{ kind: "isol8.hire_suggestion", reason: "r", listings: FIVE_LISTINGS }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /4 alternatives/i }));
    await userEvent.click(screen.getByRole("button", { name: /make agent 3 the recommendation/i }));

    // The picked listing is now the one and only card with a Deploy button.
    const deployButtons = screen.getAllByRole("button", { name: /deploy/i });
    expect(deployButtons).toHaveLength(1);
    expect(screen.getByRole("article", { name: "Agent 3" })).toBeInTheDocument();

    // The defect this task exists to prevent: browsing/promoting an
    // alternative must never itself hire anything.
    expect(post).not.toHaveBeenCalled();
  });

  it("resets deploy state on promotion — a stale 'Added to your team' must not survive it", async () => {
    // Regression: PrimaryListing carried local deploy state with no `key`,
    // so React reused the same instance across a promotion and the new
    // primary inherited the old one's "done" state — rendering an agent
    // that was never deployed as already-deployed and permanently
    // un-deployable.
    render(
      <HireCard
        suggestion={{ kind: "isol8.hire_suggestion", reason: "r", listings: FIVE_LISTINGS }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /deploy/i }));
    await waitFor(() => expect(screen.getByRole("button", { name: /added/i })).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /4 alternatives/i }));
    await userEvent.click(screen.getByRole("button", { name: /make agent 3 the recommendation/i }));

    // The newly-promoted card must be a fresh, actionable Deploy button —
    // not a leaked "Added to your team" it never earned.
    const deployButton = screen.getByRole("button", { name: /deploy to my team/i });
    expect(deployButton).toBeInTheDocument();
    expect(deployButton).not.toBeDisabled();
    expect(screen.queryByRole("button", { name: /added/i })).not.toBeInTheDocument();
  });

  it("folds the old primary back into the alternatives after a promotion", async () => {
    render(
      <HireCard
        suggestion={{ kind: "isol8.hire_suggestion", reason: "r", listings: FIVE_LISTINGS }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /4 alternatives/i }));
    await userEvent.click(screen.getByRole("button", { name: /make agent 3 the recommendation/i }));

    // Promoting closes the drawer (still collapsed by default) — reopen it.
    await userEvent.click(screen.getByRole("button", { name: /4 alternatives/i }));

    // The old primary (Agent 1) is back among the alternatives...
    expect(screen.getByRole("article", { name: "Agent 1" })).toBeInTheDocument();
    // ...and Agent 3 appears exactly once — as the new primary, not
    // duplicated into the alternatives list it just left.
    expect(screen.getAllByText("Agent 3")).toHaveLength(1);
  });

  it("collapses the drawer by default", () => {
    // Mobile constraint: the recommendation plus the agent's next sentence
    // need to fit one screen, so the drawer never starts open.
    render(
      <HireCard
        suggestion={{ kind: "isol8.hire_suggestion", reason: "r", listings: FIVE_LISTINGS }}
      />,
    );
    expect(screen.getByRole("button", { name: /4 alternatives/i })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByText("Agent 2")).not.toBeInTheDocument();
  });
});
