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
  createProps: vi.fn(), selectProps: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: clerk.replace, push: clerk.replace }), useSearchParams: () => new URLSearchParams() }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: clerk.loaded, isSignedIn: true, orgId: clerk.orgId }),
  useUser: () => ({ user: { unsafeMetadata: {}, update: clerk.update } }),
  useOrganization: () => ({ isLoaded: clerk.loaded, organization: clerk.organization }),
  useOrganizationList: () => ({ isLoaded: clerk.loaded, setActive: clerk.setActive,
    userMemberships: { data: clerk.listsPresent ? clerk.memberships : undefined, isLoading: false, revalidate: clerk.revalidateMemberships },
    userInvitations: { data: clerk.listsPresent ? clerk.invitations : undefined, isLoading: false, revalidate: clerk.revalidateInvitations },
  }),
  CreateOrganization: (props: unknown) => { clerk.createProps(props); return null; },
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
    expect(clerk.createProps).not.toHaveBeenCalled();
    expect(clerk.replace).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
  });
  it("offers workspace creation without requiring a signup intent", () => {
    render(<OnboardingPage />);
    expect(screen.getByRole("heading", { name: "Create your workspace" })).toBeInTheDocument();
    expect(clerk.createProps).toHaveBeenCalledWith(expect.objectContaining({ afterCreateOrganizationUrl: "/onboarding", skipInvitationScreen: true }));
    expect(clerk.replace).not.toHaveBeenCalled();
  });
  it("creates the workspace under one heading, without Clerk's own header or logo step", () => {
    render(<OnboardingPage />);
    expect(screen.getAllByRole("heading")).toHaveLength(1);
    expect(clerk.createProps).toHaveBeenCalledWith(expect.objectContaining({
      appearance: { elements: expect.objectContaining({ header: { display: "none" }, organizationAvatarUploaderContainer: { display: "none" } }) },
    }));
  });
  it("sits on the brand ground even after a client navigation from the themed chat", () => {
    const root = document.documentElement;
    root.setAttribute("data-chat-theme", "dark");
    root.setAttribute("data-chat-accent", "focus");
    const { unmount } = render(<OnboardingPage />);
    expect(root.hasAttribute("data-chat-theme")).toBe(false);
    expect(root.hasAttribute("data-chat-accent")).toBe(false);
    // The way back into /chat is a client navigation: its inline theme
    // bootstrap won't run again, so onboarding must hand the theme back.
    unmount();
    expect(root.getAttribute("data-chat-theme")).toBe("dark");
    expect(root.getAttribute("data-chat-accent")).toBe("focus");
    root.removeAttribute("data-chat-theme"); root.removeAttribute("data-chat-accent");
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
    expect(clerk.createProps).not.toHaveBeenCalled();
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
