// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useOnboardingGate } from "../useOnboardingGate";

const clerk = vi.hoisted(() => ({
  listsPresent: true, loaded: true, signedIn: true, orgId: null as string | null,
  organization: null as { id: string } | null,
  memberships: [] as Array<{ organization: { id: string } }>,
  invitations: [] as Array<{ id: string }>,
  listLoading: false,
  setActive: vi.fn().mockResolvedValue(undefined), replace: vi.fn(), update: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: clerk.replace }), useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: clerk.loaded, isSignedIn: clerk.signedIn, orgId: clerk.orgId }),
  useUser: () => ({ isLoaded: clerk.loaded, user: { unsafeMetadata: {}, update: clerk.update } }),
  useOrganization: () => ({ isLoaded: clerk.loaded, organization: clerk.organization }),
  useOrganizationList: () => ({ isLoaded: clerk.loaded, setActive: clerk.setActive,
    userMemberships: { data: clerk.listsPresent ? clerk.memberships : undefined, isLoading: clerk.listLoading },
    userInvitations: { data: clerk.listsPresent ? clerk.invitations : undefined, isLoading: clerk.listLoading },
  }),
}));
beforeEach(() => {
  Object.assign(clerk, { listsPresent: true, loaded: true, signedIn: true, orgId: null, organization: null,
    memberships: [], invitations: [], listLoading: false });
  vi.clearAllMocks();
  clerk.setActive.mockResolvedValue(undefined);
  clerk.update.mockResolvedValue(undefined);
});
describe("workspace onboarding gate", () => {
  it.each(["hydrating", "signed out", "lists loading", "lists unavailable"])("holds while %s", (state) => {
    clerk.loaded = state !== "hydrating";
    clerk.signedIn = state !== "signed out";
    clerk.listLoading = state === "lists loading";
    clerk.listsPresent = state !== "lists unavailable";
    expect(renderHook(useOnboardingGate).result.current).toBe("loading");
    expect(clerk.replace).not.toHaveBeenCalled();
  });
  it("routes an account without memberships to workspace creation without metadata writes", () => {
    expect(renderHook(useOnboardingGate).result.current).toBe("redirecting");
    expect(clerk.replace).toHaveBeenCalledWith("/onboarding");
    expect(clerk.update).not.toHaveBeenCalled();
  });
  it.each([false, true])("pending invitations win even with an existing active workspace: %s", (active) => {
    clerk.invitations = [{ id: "inv_1" }];
    clerk.memberships = [{ organization: { id: "org_old" } }];
    if (active) { clerk.organization = { id: "org_old" }; clerk.orgId = "org_old"; }
    expect(renderHook(useOnboardingGate).result.current).toBe("redirecting");
    expect(clerk.replace).toHaveBeenCalledWith("/onboarding");
    expect(clerk.setActive).not.toHaveBeenCalled();
  });
  it("activates a sole membership and holds until both Clerk contexts agree", () => {
    clerk.memberships = [{ organization: { id: "org_only" } }];
    const { result, rerender } = renderHook(useOnboardingGate);
    expect(result.current).toBe("loading");
    expect(clerk.setActive).toHaveBeenCalledWith({ organization: "org_only" });
    clerk.organization = { id: "org_only" }; rerender();
    expect(result.current).toBe("loading");
    clerk.orgId = "org_only"; rerender();
    expect(result.current).toBe("ready");
  });
  it("requires selection for multiple memberships", () => {
    clerk.memberships = [{ organization: { id: "org_a" } }, { organization: { id: "org_b" } }];
    expect(renderHook(useOnboardingGate).result.current).toBe("selecting");
    expect(clerk.setActive).not.toHaveBeenCalled();
  });
  it("allows an active workspace only when the auth and UI organization agree", () => {
    clerk.organization = { id: "org_a" }; clerk.orgId = "org_b";
    const { result, rerender } = renderHook(useOnboardingGate);
    expect(result.current).toBe("loading");
    clerk.orgId = "org_a"; rerender();
    expect(result.current).toBe("ready");
  });
});
