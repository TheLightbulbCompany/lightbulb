// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import OnboardingPage from "../page";

const clerk = vi.hoisted(() => ({
  loaded: true, replace: vi.fn(), update: vi.fn(), firstName: "Ada" as string | null,
  apiGet: vi.fn(), apiPost: vi.fn(), save: vi.fn(),
  config: { business: "", onboardingStep: undefined } as Record<string, unknown> | undefined,
  configError: undefined as Error | undefined,
}));
vi.mock("@/lib/api", async () => {
  const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
  return { ApiError, useApi: () => ({ get: clerk.apiGet, post: clerk.apiPost }) };
});
vi.mock("@/components/voice/data", () => ({ useAgentConfig: () => ({ data: clerk.config, error: clerk.configError, save: clerk.save }) }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: clerk.replace, push: clerk.replace }), useSearchParams: () => new URLSearchParams() }));
const analytics = vi.hoisted(() => ({ capture: vi.fn(), clearWorkspaceDoor: vi.fn() }));
vi.mock("@/lib/analytics", () => ({
  capture: analytics.capture,
  touchPayload: () => ({ first_touch: { utm_source: "reddit" }, last_touch: {} }),
  readWorkspaceDoor: () => null,
  clearWorkspaceDoor: analytics.clearWorkspaceDoor,
}));
// Only useUser (the name gate, the user id) is read from Clerk: any organization hook is undefined here and throws.
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({ isLoaded: clerk.loaded, user: { id: "user_a", unsafeMetadata: {}, update: clerk.update, firstName: clerk.firstName } }),
}));
beforeEach(() => {
  Object.assign(clerk, { loaded: true, firstName: "Ada", config: { business: "", onboardingStep: undefined }, configError: undefined });
  vi.clearAllMocks();
  clerk.save.mockResolvedValue(undefined);
  localStorage.clear();
});
describe("business onboarding", () => {
  it("asks a person with no name for one before the business form", () => {
    clerk.firstName = null;
    window.history.replaceState(null, "", "/onboarding");
    render(<OnboardingPage />);
    expect(clerk.replace).toHaveBeenCalledWith("/welcome/name?returnTo=%2Fonboarding");
    expect(clerk.replace).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText("Business name")).not.toBeInTheDocument();
    expect(clerk.apiPost).not.toHaveBeenCalled();
  });
  it("test_onboarding_claims_slug_with_name_and_no_clerk_call", async () => {
    clerk.apiGet.mockImplementation(async (path: string) => ({
      slug: new URLSearchParams(path.split("?")[1]).get("slug"), available: true, reason: null, domain: "dev.thelightbulb.company",
    }));
    clerk.apiPost.mockResolvedValue({ slug: "bench-club" });
    render(<OnboardingPage />);
    expect(screen.getByRole("heading", { level: 1, name: "Set up your business" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Paste a join link")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Bench Club!" } });
    // The address is one line with "Change", not a second field to fill in.
    await waitFor(() => expect(screen.getByText("agents@bench-club.dev.thelightbulb.company")).toBeInTheDocument());
    expect(screen.queryByLabelText("Meeting invite address")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/How did you first hear about us/), { target: { value: "reddit" } });
    fireEvent.change(screen.getByLabelText("Tell us more"), { target: { value: "r/startups" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(clerk.replace).toHaveBeenCalledWith("/voice"));
    expect(clerk.apiPost.mock.calls).toEqual([
      ["/company/source", { answer: "reddit", detail: "r/startups", door: "sign_up", first_touch: { utm_source: "reddit" }, last_touch: {} }],
      ["/company/slug/hold", { slug: "bench-club" }],
      ["/company/slug/claim", { slug: "bench-club", name: "Bench Club!" }],
    ]);
    // The name is the business the assistant's setup starts from.
    expect(clerk.save).toHaveBeenCalledWith({ business: "Bench Club!", onboardingStep: 1 });
    expect(analytics.clearWorkspaceDoor).toHaveBeenCalled();
    expect(analytics.capture).toHaveBeenCalledWith("source_reported", expect.objectContaining({
      surface: "sign_up", answer: "reddit", has_detail: true, $set_once: { self_reported_source: "reddit" },
    }));
  });
  it("keeps an address the account already claimed and still saves the business", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    clerk.apiGet.mockImplementation(async () => ({ slug: "acme", available: true, reason: null, domain: "thelightbulb.company" }));
    clerk.apiPost.mockImplementation(async (path: string) => {
      if (path === "/company/slug/claim") throw new ApiError(409, { detail: "slug_already_claimed" }, "slug_already_claimed");
      return {};
    });
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Acme" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(clerk.replace).toHaveBeenCalledWith("/voice"));
    expect(clerk.save).toHaveBeenCalledWith({ business: "Acme", onboardingStep: 1 });
  });
  it("waits for the assistant's document before it can continue", async () => {
    clerk.config = undefined;
    clerk.apiGet.mockImplementation(async () => ({ slug: "acme", available: true, reason: null, domain: "thelightbulb.company" }));
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Acme" } });
    await waitFor(() => expect(screen.getByText("agents@acme.thelightbulb.company")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
  });
  it("sends an account that already has a business back to /voice before any write", async () => {
    clerk.config = { business: "Acme", live: true };
    clerk.apiGet.mockImplementation(async () => ({ slug: "other", available: true, reason: null, domain: "thelightbulb.company" }));
    render(<OnboardingPage />);
    await waitFor(() => expect(clerk.replace).toHaveBeenCalledWith("/voice"));
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Other" } });
    await waitFor(() => expect(screen.getByText("agents@other.thelightbulb.company")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    fireEvent.submit(screen.getByRole("button", { name: "Continue" }).closest("form")!);
    expect(clerk.apiPost).not.toHaveBeenCalled();
    expect(clerk.save).not.toHaveBeenCalled();
  });
  it("writes nothing to the assistant when the claim fails, and says so", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    clerk.apiGet.mockImplementation(async () => ({ slug: "acme", available: true, reason: null, domain: "thelightbulb.company" }));
    clerk.apiPost.mockImplementation(async (path: string) => {
      if (path === "/company/slug/claim") throw new ApiError(500, { detail: "boom" }, "boom");
      return {};
    });
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Acme" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("We couldn't set up your business. Please try again.");
    expect(clerk.save).not.toHaveBeenCalled();
    expect(clerk.replace).not.toHaveBeenCalled();
  });
  it("says so when the assistant's setup cannot be read, instead of a silent disabled button", () => {
    clerk.config = undefined;
    clerk.configError = new Error("offline");
    render(<OnboardingPage />);
    expect(screen.getByRole("alert")).toHaveTextContent("We couldn't load your setup. Reload the page to try again.");
  });
  it("claim went through, the save failed, the page reloaded: setup finishes with the same address and no second slug", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    let claimedBy: string | null = null; // the server's slug table, for one slug
    clerk.apiGet.mockImplementation(async (path: string) => {
      const slug = new URLSearchParams(path.split("?")[1]).get("slug");
      return { slug, available: !claimedBy, reason: claimedBy ? "taken" : null, domain: "thelightbulb.company" };
    });
    clerk.apiPost.mockImplementation(async (path: string) => {
      if (path === "/company/slug/hold" && claimedBy) throw new ApiError(409, { detail: "slug_taken" }, "slug_taken");
      if (path === "/company/slug/claim") claimedBy = "user_a"; // replays for the same tenant succeed
      return {};
    });
    clerk.save.mockRejectedValueOnce(new Error("offline"));
    const first = render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Acme" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("We couldn't set up your business. Please try again.");
    first.unmount(); // reload

    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Acme" } });
    await waitFor(() => expect(screen.getByText("agents@acme.thelightbulb.company")).toBeInTheDocument());
    expect(screen.queryByText("That address is taken. Try another.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(clerk.replace).toHaveBeenCalledWith("/voice"));
    const slugs = clerk.apiPost.mock.calls.filter(([p]) => p !== "/company/source").map(([p, b]) => [p, (b as { slug: string }).slug]);
    expect(slugs).toEqual([["/company/slug/hold", "acme"], ["/company/slug/claim", "acme"], ["/company/slug/claim", "acme"]]);
    expect(clerk.save).toHaveBeenLastCalledWith({ business: "Acme", onboardingStep: 1 });
  });
  it("on another device, a hold refused for the person's own claimed address is confirmed by the claim", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    clerk.apiGet.mockImplementation(async () => ({ slug: "acme", available: true, reason: null, domain: "thelightbulb.company" }));
    clerk.apiPost.mockImplementation(async (path: string) => {
      if (path === "/company/slug/hold") throw new ApiError(409, { detail: "slug_taken" }, "slug_taken");
      return {};
    });
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Acme" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(clerk.replace).toHaveBeenCalledWith("/voice"));
    expect(clerk.apiPost).toHaveBeenCalledWith("/company/slug/claim", { slug: "acme", name: "Acme" });
  });
  it("lets the owner edit the slug, and blocks creation while it is taken", async () => {
    clerk.apiGet.mockImplementation(async (path: string) => {
      const slug = new URLSearchParams(path.split("?")[1]).get("slug");
      return { slug, available: slug !== "acme", reason: slug === "acme" ? "taken" : null, domain: "thelightbulb.company" };
    });
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Acme" } });
    // A taken address opens the field by itself.
    await waitFor(() => expect(screen.getByText("That address is taken. Try another.")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Meeting invite address"), { target: { value: "acme-labs" } });
    await waitFor(() => expect(screen.getByText("agents@acme-labs.thelightbulb.company")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled();
    // Clearing the field goes back to the name's suggestion.
    fireEvent.change(screen.getByLabelText("Meeting invite address"), { target: { value: "" } });
    expect(screen.getByLabelText("Meeting invite address")).toHaveValue("acme");
  });
  it("opens the address field on Change", async () => {
    clerk.apiGet.mockImplementation(async (path: string) => ({
      slug: new URLSearchParams(path.split("?")[1]).get("slug"), available: true, reason: null, domain: "thelightbulb.company",
    }));
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Acme" } });
    fireEvent.click(await screen.findByRole("button", { name: "Change" }));
    expect(screen.getByLabelText("Meeting invite address")).toHaveValue("acme");
  });
  it("ignores an availability answer for a slug the user has already typed past", async () => {
    let answerFirst: (v: unknown) => void = () => {};
    clerk.apiGet.mockImplementation((path: string) => {
      const slug = new URLSearchParams(path.split("?")[1]).get("slug");
      const answer = { slug, available: true, reason: null, domain: "thelightbulb.company" };
      return slug === "ab" ? new Promise((resolve) => { answerFirst = () => resolve(answer); }) : Promise.resolve(answer);
    });
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "ab" } });
    await waitFor(() => expect(clerk.apiGet).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "abc" } });
    await waitFor(() => expect(screen.getByText("agents@abc.thelightbulb.company")).toBeInTheDocument());
    answerFirst(undefined);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByText("agents@abc.thelightbulb.company")).toBeInTheDocument();
  });
  it("a lost race says so and saves nothing", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    clerk.apiGet.mockImplementation(async () => ({ slug: "acme", available: true, reason: null, domain: "thelightbulb.company" }));
    clerk.apiPost.mockRejectedValue(new ApiError(409, { detail: "slug_taken" }, "slug_taken"));
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Acme" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(screen.getByText("That address is taken. Try another.")).toBeInTheDocument());
    expect(clerk.save).not.toHaveBeenCalled();
    expect(clerk.replace).not.toHaveBeenCalled();
  });
});
