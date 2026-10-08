// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import OnboardingPage from "../page";

const clerk = vi.hoisted(() => ({
  listsPresent: true, loaded: true, orgId: null as string | null, organization: null as { id: string } | null,
  memberships: [] as Array<{ organization: { id: string } }>,
  invitations: [] as Array<{ id: string; publicOrganizationData: { id: string; name: string }; accept: () => Promise<unknown> }>,
  setActive: vi.fn().mockResolvedValue(undefined), replace: vi.fn(), update: vi.fn(),
  revalidateInvitations: vi.fn().mockResolvedValue(undefined), revalidateMemberships: vi.fn().mockResolvedValue(undefined),
  selectProps: vi.fn(), createOrganization: vi.fn(),
  apiGet: vi.fn(), apiPost: vi.fn(),
}));
vi.mock("@/lib/api", async () => {
  const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
  return { ApiError, useApi: () => ({ get: clerk.apiGet, post: clerk.apiPost }) };
});
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: clerk.replace, push: clerk.replace }), useSearchParams: () => new URLSearchParams() }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: clerk.loaded, isSignedIn: true, orgId: clerk.orgId }),
  useUser: () => ({ user: { unsafeMetadata: {}, update: clerk.update } }),
  useOrganization: () => ({ isLoaded: clerk.loaded, organization: clerk.organization }),
  useOrganizationList: () => ({ isLoaded: clerk.loaded, setActive: clerk.setActive, createOrganization: clerk.createOrganization,
    userMemberships: { data: clerk.listsPresent ? clerk.memberships : undefined, isLoading: false, revalidate: clerk.revalidateMemberships },
    userInvitations: { data: clerk.listsPresent ? clerk.invitations : undefined, isLoading: false, revalidate: clerk.revalidateInvitations },
  }),
  OrganizationList: (props: unknown) => { clerk.selectProps(props); return null; },
}));
beforeEach(() => {
  Object.assign(clerk, { listsPresent: true, loaded: true, orgId: null, organization: null, memberships: [], invitations: [] });
  vi.clearAllMocks();
  clerk.setActive.mockResolvedValue(undefined);
  clerk.update.mockResolvedValue(undefined);
  clerk.revalidateInvitations.mockResolvedValue(undefined);
  clerk.revalidateMemberships.mockResolvedValue(undefined);
});
describe("workspace onboarding", () => {
  it("does not offer creation until membership and invitation lists are available", () => {
    clerk.listsPresent = false;
    render(<OnboardingPage />);
    expect(screen.queryByLabelText("Workspace name")).not.toBeInTheDocument();
    expect(clerk.replace).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
  });
  it("puts the create form first and the join option under it, without requiring a signup intent", () => {
    render(<OnboardingPage />);
    expect(screen.getByRole("heading", { level: 1, name: "Create your workspace" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading").map((h) => h.textContent)).toEqual(["Create your workspace", "Joining a team instead?"]);
    expect(screen.getByText("Invited by email? Open that email on this device and tap its link.")).toBeInTheDocument();
    const name = screen.getByLabelText("Workspace name");
    expect(name.compareDocumentPosition(screen.getByLabelText("Paste a join link")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("button", { name: "Create workspace" })).toBeDisabled();
    expect(clerk.replace).not.toHaveBeenCalled();
  });
  it.each([
    ["https://thelightbulb.company/join/org_abc123#t0k3n", "/join/org_abc123#t0k3n"],
    ["  /join/org_abc123#t0k3n ", "/join/org_abc123#t0k3n"],
  ])("routes a pasted join link %j to the join page", (pasted, target) => {
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Paste a join link"), { target: { value: pasted } });
    fireEvent.click(screen.getByRole("button", { name: "Join" }));
    expect(clerk.replace).toHaveBeenCalledWith(target);
  });
  it.each(["hello", "https://thelightbulb.company/join/org_abc123", "https://example.com/chat#x"])("refuses %j as a join link", (pasted) => {
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Paste a join link"), { target: { value: pasted } });
    fireEvent.click(screen.getByRole("button", { name: "Join" }));
    expect(screen.getByRole("alert")).toHaveTextContent("That doesn't look like a Lightbulb join link.");
    expect(clerk.replace).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Workspace name")).toBeInTheDocument();
  });
  it("previews the agents' address from the name, then holds the slug BEFORE creating the org", async () => {
    clerk.apiGet.mockImplementation(async (path: string) => ({
      slug: new URLSearchParams(path.split("?")[1]).get("slug"), available: true, reason: null, domain: "dev.thelightbulb.company",
    }));
    clerk.apiPost.mockResolvedValue({ slug: "bench-club" });
    clerk.createOrganization.mockResolvedValue({ id: "org_new" });
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Workspace name"), { target: { value: "Bench Club!" } });
    expect(screen.getByLabelText("Agent email address")).toHaveValue("bench-club");
    await waitFor(() => expect(screen.getByText("ceo@bench-club.dev.thelightbulb.company")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));
    await waitFor(() => expect(clerk.setActive).toHaveBeenCalledWith({ organization: "org_new" }));
    expect(clerk.apiPost).toHaveBeenCalledWith("/company/slug/hold", { slug: "bench-club" });
    await waitFor(() => expect(clerk.apiPost).toHaveBeenCalledWith("/company/slug/claim", { slug: "bench-club" }));
    expect(clerk.apiPost.mock.invocationCallOrder[0]).toBeLessThan(clerk.createOrganization.mock.invocationCallOrder[0]);
    expect(clerk.createOrganization).toHaveBeenCalledWith({ name: "Bench Club!" });
  });
  it("lets the owner edit the slug, and blocks creation while it is taken", async () => {
    clerk.apiGet.mockImplementation(async (path: string) => {
      const slug = new URLSearchParams(path.split("?")[1]).get("slug");
      return { slug, available: slug !== "acme", reason: slug === "acme" ? "taken" : null, domain: "thelightbulb.company" };
    });
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Workspace name"), { target: { value: "Acme" } });
    await waitFor(() => expect(screen.getByText("That address is taken. Try another.")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Create workspace" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Agent email address"), { target: { value: "acme-labs" } });
    await waitFor(() => expect(screen.getByText("ceo@acme-labs.thelightbulb.company")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Create workspace" })).toBeEnabled();
    // Clearing the field goes back to the name's suggestion.
    fireEvent.change(screen.getByLabelText("Agent email address"), { target: { value: "" } });
    expect(screen.getByLabelText("Agent email address")).toHaveValue("acme");
  });
  it("ignores an availability answer for a slug the user has already typed past", async () => {
    let answerFirst: (v: unknown) => void = () => {};
    clerk.apiGet.mockImplementation((path: string) => {
      const slug = new URLSearchParams(path.split("?")[1]).get("slug");
      const answer = { slug, available: true, reason: null, domain: "thelightbulb.company" };
      return slug === "ab" ? new Promise((resolve) => { answerFirst = () => resolve(answer); }) : Promise.resolve(answer);
    });
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Workspace name"), { target: { value: "ab" } });
    await waitFor(() => expect(clerk.apiGet).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText("Workspace name"), { target: { value: "abc" } });
    await waitFor(() => expect(screen.getByText("ceo@abc.thelightbulb.company")).toBeInTheDocument());
    answerFirst(undefined);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.getByText("ceo@abc.thelightbulb.company")).toBeInTheDocument();
  });
  it("a lost race says so and never creates the org", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    clerk.apiGet.mockImplementation(async () => ({ slug: "acme", available: true, reason: null, domain: "thelightbulb.company" }));
    clerk.apiPost.mockRejectedValue(new ApiError(409, { detail: "slug_taken" }, "slug_taken"));
    render(<OnboardingPage />);
    fireEvent.change(screen.getByLabelText("Workspace name"), { target: { value: "Acme" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Create workspace" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Create workspace" }));
    await waitFor(() => expect(screen.getByText("That address is taken. Try another.")).toBeInTheDocument());
    expect(clerk.createOrganization).not.toHaveBeenCalled();
  });
  it("offers Clerk selection instead of activating an arbitrary membership", () => {
    clerk.memberships = [{ organization: { id: "org_a" } }, { organization: { id: "org_b" } }];
    render(<OnboardingPage />);
    expect(screen.getByRole("heading", { name: "Choose your workspace" })).toBeInTheDocument();
    expect(clerk.selectProps).toHaveBeenCalledWith(expect.objectContaining({ hidePersonal: true, afterSelectOrganizationUrl: "/onboarding" }));
    expect(clerk.setActive).not.toHaveBeenCalled();
  });
  it("activates the only membership, then waits for UI and auth agreement before continuing", async () => {
    clerk.memberships = [{ organization: { id: "org_a" } }];
    const { rerender } = render(<OnboardingPage />);
    await waitFor(() => expect(clerk.setActive).toHaveBeenCalledWith({ organization: "org_a" }));
    expect(clerk.replace).not.toHaveBeenCalled();
    clerk.organization = { id: "org_a" }; rerender(<OnboardingPage />);
    expect(clerk.replace).not.toHaveBeenCalled();
    clerk.orgId = "org_a"; rerender(<OnboardingPage />);
    await waitFor(() => expect(clerk.replace).toHaveBeenCalledWith("/chat"));
    expect(clerk.update).not.toHaveBeenCalled();
  });
  it("accepts invitations before any existing workspace and activates the invited workspace", async () => {
    clerk.organization = { id: "org_old" }; clerk.orgId = "org_old";
    clerk.memberships = [{ organization: { id: "org_old" } }];
    const accept = vi.fn().mockResolvedValue(undefined);
    clerk.invitations = [{ id: "inv_1", publicOrganizationData: { id: "org_invited", name: "Invited workspace" }, accept }];
    render(<OnboardingPage />);
    expect(clerk.replace).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(clerk.setActive).toHaveBeenCalledWith({ organization: "org_invited" }));
    expect(accept).toHaveBeenCalledOnce();
    expect(clerk.revalidateInvitations).toHaveBeenCalled();
    expect(clerk.revalidateMemberships).toHaveBeenCalled();
    expect(screen.queryByLabelText("Workspace name")).not.toBeInTheDocument();
    expect(clerk.update).not.toHaveBeenCalled();
  });
  it("shows an actionable error when invitation acceptance fails", async () => {
    clerk.invitations = [{ id: "inv_1", publicOrganizationData: { id: "org_invited", name: "Invited workspace" }, accept: vi.fn().mockRejectedValue(new Error("offline")) }];
    render(<OnboardingPage />);
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept" })).toBeEnabled();
    expect(clerk.setActive).not.toHaveBeenCalled();
  });
  it("holds the invited workspace through activation failure instead of redirecting to the old one", async () => {
    clerk.organization = { id: "org_old" }; clerk.orgId = "org_old";
    clerk.memberships = [{ organization: { id: "org_old" } }];
    clerk.invitations = [{ id: "inv_1", publicOrganizationData: { id: "org_invited", name: "Invited workspace" }, accept: vi.fn().mockResolvedValue(undefined) }];
    clerk.revalidateInvitations.mockImplementation(async () => { clerk.invitations = []; });
    clerk.setActive.mockRejectedValueOnce(new Error("offline"));
    render(<OnboardingPage />);
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(clerk.replace).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(clerk.setActive).toHaveBeenCalledTimes(2));
    expect(clerk.setActive).toHaveBeenLastCalledWith({ organization: "org_invited" });
  });
  it("allows a second invitation after the first workspace has been activated", async () => {
    const accepted = vi.fn().mockResolvedValue(undefined);
    clerk.invitations = [
      { id: "inv_1", publicOrganizationData: { id: "org_a", name: "A" }, accept: accepted },
      { id: "inv_2", publicOrganizationData: { id: "org_b", name: "B" }, accept: accepted },
    ];
    clerk.revalidateInvitations.mockImplementation(async () => { clerk.invitations = clerk.invitations.slice(1); });
    render(<OnboardingPage />);
    fireEvent.click(screen.getAllByRole("button", { name: "Accept" })[0]);
    await waitFor(() => expect(clerk.setActive).toHaveBeenCalledWith({ organization: "org_a" }));
    expect(screen.getByRole("button", { name: "Accept" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(clerk.setActive).toHaveBeenCalledWith({ organization: "org_b" }));
  });
});
