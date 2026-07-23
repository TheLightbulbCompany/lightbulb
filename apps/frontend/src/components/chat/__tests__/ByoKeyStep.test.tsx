import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// vi.mock is hoisted ABOVE module-level consts, so anything the factory
// closes over has to come through vi.hoisted().
const { putMock, postMock } = vi.hoisted(() => ({
  putMock: vi.fn(),
  postMock: vi.fn(),
}));

// Spread the real module so the `ApiError` re-export stays the actual class —
// the component does `err instanceof ApiError`, so the errors these tests
// throw must be that same identity.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    useApi: () => ({ put: putMock, post: postMock, get: vi.fn(), del: vi.fn() }),
  };
});

// The step now reads useSearchParams (for the "choose a different provider"
// escape). Mirror the CreditsStep test's next/navigation mock.
let searchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => searchParams,
}));

import { ApiError } from "@/lib/api";
import { ByoKeyStep } from "../ByoKeyStep";

/** Mirrors ProvisioningStepper's blocked-card copy for a non-admin member. */
const ADMIN_ONLY_MESSAGE = "Ask your org admin to fix this.";

function fillKeyAndSave(key: string, placeholder = "sk-proj-...") {
  fireEvent.change(screen.getByPlaceholderText(placeholder), {
    target: { value: key },
  });
  fireEvent.click(screen.getByRole("button", { name: /save key/i }));
}

describe("ByoKeyStep", () => {
  beforeEach(() => {
    putMock.mockReset().mockResolvedValue({});
    postMock.mockReset().mockResolvedValue({});
    searchParams = new URLSearchParams();
  });

  it("strips ?provider= when choosing a different provider, preserving other params", () => {
    // The escape hatch out of a mistakenly-picked BYO step — mirrors
    // CreditsStep. The strip commits through the History API (shellNav).
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");
    searchParams = new URLSearchParams({ provider: "byo_key", checkout: "success" });
    render(<ByoKeyStep onComplete={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /choose a different provider/i }));
    const target = replaceStateSpy.mock.calls.at(-1)?.[2] as string;
    expect(target).not.toMatch(/provider=/);
    expect(target).toMatch(/checkout=success/);
    replaceStateSpy.mockRestore();
  });

  it("saves the key, commits the provider choice, then completes", async () => {
    const onComplete = vi.fn();
    render(<ByoKeyStep onComplete={onComplete} />);

    fireEvent.click(screen.getByRole("radio", { name: /anthropic/i }));
    fillKeyAndSave("sk-ant-secret", "sk-ant-...");

    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    expect(putMock).toHaveBeenCalledWith("/settings/keys/anthropic", {
      api_key: "sk-ant-secret",
    });
    // /billing/provider — NOT /users/sync, which the backend accepts and then
    // silently ignores, leaving byo_provider unset and the gate re-firing.
    expect(postMock).toHaveBeenCalledWith("/billing/provider", {
      provider_choice: "byo_key",
      byo_provider: "anthropic",
    });
    expect(postMock).not.toHaveBeenCalledWith(
      "/users/sync",
      expect.anything(),
    );
    // Order is required: change_provider 409s with byo_key_missing unless the
    // key PUT already landed.
    expect(putMock.mock.invocationCallOrder[0]).toBeLessThan(
      postMock.mock.invocationCallOrder[0],
    );
  });

  it("defaults to openai and carries that through to the provider write", async () => {
    const onComplete = vi.fn();
    render(<ByoKeyStep onComplete={onComplete} />);

    fillKeyAndSave("sk-proj-abc");

    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    expect(putMock).toHaveBeenCalledWith("/settings/keys/openai", {
      api_key: "sk-proj-abc",
    });
    expect(postMock).toHaveBeenCalledWith("/billing/provider", {
      provider_choice: "byo_key",
      byo_provider: "openai",
    });
  });

  // REGRESSION GUARD. The provider write is the ONLY thing that clears the
  // byo_provider_required gate; completing without it re-polls straight back
  // into the same gate, whose remedy link routes here again — the dead end.
  it("does not complete when the provider write fails for a non-subscription reason", async () => {
    postMock.mockRejectedValue(new Error("network down"));
    const onComplete = vi.fn();
    render(<ByoKeyStep onComplete={onComplete} />);

    fillKeyAndSave("sk-proj-abc");

    await waitFor(() => expect(screen.getByText("network down")).toBeInTheDocument());
    expect(putMock).toHaveBeenCalledTimes(1);
    expect(onComplete).not.toHaveBeenCalled();
    // Still retryable — the key PUT is idempotent, so a retry resumes here.
    expect(screen.getByRole("button", { name: /save key/i })).not.toBeDisabled();
  });

  it("does not complete on a structured 500 from the provider write, and shows its message", async () => {
    postMock.mockRejectedValue(
      new ApiError(500, { detail: "billing backend exploded" }, "billing backend exploded"),
    );
    const onComplete = vi.fn();
    render(<ByoKeyStep onComplete={onComplete} />);

    fillKeyAndSave("sk-proj-abc");

    await waitFor(() =>
      expect(screen.getByText("billing backend exploded")).toBeInTheDocument(),
    );
    expect(onComplete).not.toHaveBeenCalled();
  });

  // Pre-subscription softness. The signup wizard can reach this step before a
  // subscription exists (a trial_local owner does), and change_provider
  // refuses to write on those rows. The key IS saved and the gate will route
  // the user back here once subscribed, so blocking the wizard would just
  // trade one dead end for another.
  const preSubscriptionCases: Array<{ status: number; code: string; message: string }> = [
    {
      status: 409,
      code: "use_trial_checkout",
      message: "Pick a plan first — provider selection happens at checkout for free-tier accounts.",
    },
    {
      status: 409,
      code: "subscription_inactive",
      message: "Subscription is not active. Manage it in /settings/billing.",
    },
    {
      status: 404,
      code: "no_billing_account",
      message: "No billing account. Subscribe via /onboarding first.",
    },
  ];
  it.each(preSubscriptionCases)(
    "completes anyway, silently, on a $status $code from the provider write",
    async ({ status, code, message }) => {
      postMock.mockRejectedValue(
        new ApiError(status, { detail: { code, message } }, `API ${status}`),
      );
      const onComplete = vi.fn();
      render(<ByoKeyStep onComplete={onComplete} />);

      fillKeyAndSave("sk-proj-abc");

      await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
      // The key landed; the deferred commit is not the user's problem.
      expect(putMock).toHaveBeenCalledTimes(1);
      expect(screen.queryByText(message)).not.toBeInTheDocument();
    },
  );

  it("shows an error and never writes the provider when the key save fails", async () => {
    putMock.mockRejectedValue(new Error("key rejected"));
    const onComplete = vi.fn();
    render(<ByoKeyStep onComplete={onComplete} />);

    fillKeyAndSave("sk-proj-abc");

    await waitFor(() => expect(screen.getByText("key rejected")).toBeInTheDocument());
    expect(postMock).not.toHaveBeenCalled();
    expect(onComplete).not.toHaveBeenCalled();
  });

  // byo_provider_required reports admin_only=false, so a non-admin org member
  // DOES reach this step — but settings_keys.set_key and
  // billing.change_provider BOTH require_org_admin, so whichever call runs
  // first 403s. The mapping is status-based, so either reads the same.
  const orgAdmin403 = () =>
    new ApiError(
      403,
      { detail: "Organization admin access required" },
      "Organization admin access required",
    );

  it("tells a non-admin member to ask their admin when the key save 403s", async () => {
    putMock.mockRejectedValue(orgAdmin403());
    const onComplete = vi.fn();
    render(<ByoKeyStep onComplete={onComplete} />);

    fillKeyAndSave("sk-proj-abc");

    await waitFor(() => expect(screen.getByText(ADMIN_ONLY_MESSAGE)).toBeInTheDocument());
    expect(screen.queryByText("Organization admin access required")).not.toBeInTheDocument();
    expect(postMock).not.toHaveBeenCalled();
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("tells a non-admin member to ask their admin when the provider write 403s", async () => {
    postMock.mockRejectedValue(orgAdmin403());
    const onComplete = vi.fn();
    render(<ByoKeyStep onComplete={onComplete} />);

    fillKeyAndSave("sk-proj-abc");

    await waitFor(() => expect(screen.getByText(ADMIN_ONLY_MESSAGE)).toBeInTheDocument());
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("surfaces the message from a structured 409 the shared client cannot lift", async () => {
    // ApiError.message is the useless "API 409" for object details — the
    // component has to dig `detail.message` out of the body itself.
    postMock.mockRejectedValue(
      new ApiError(
        409,
        { detail: { code: "byo_key_missing", message: "Save your Openai API key before switching to BYO key." } },
        "API 409",
      ),
    );
    const onComplete = vi.fn();
    render(<ByoKeyStep onComplete={onComplete} />);

    fillKeyAndSave("sk-proj-abc");

    await waitFor(() =>
      expect(
        screen.getByText("Save your Openai API key before switching to BYO key."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("API 409")).not.toBeInTheDocument();
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("recovers on retry after a transient provider-write failure", async () => {
    postMock.mockRejectedValueOnce(new Error("network down")).mockResolvedValueOnce({});
    const onComplete = vi.fn();
    render(<ByoKeyStep onComplete={onComplete} />);

    fillKeyAndSave("sk-proj-abc");
    await waitFor(() => expect(screen.getByText("network down")).toBeInTheDocument());
    expect(onComplete).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /save key/i }));
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    expect(postMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("network down")).not.toBeInTheDocument();
  });
});
