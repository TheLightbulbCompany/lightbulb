import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const syncUserMock = vi.fn().mockResolvedValue(undefined);
const pushMock = vi.fn();
let searchString = "";

// Minimal Clerk mocks — configurable invitations / memberships / org.
const buildClerkMocks = (
  overrides: {
    invitations?: unknown[];
    memberships?: unknown[];
    userUpdate?: () => Promise<void>;
    unsafeMetadata?: Record<string, unknown>;
  } = {},
) => ({
  useAuth: () => ({ isLoaded: true, orgId: null }),
  useUser: () => ({
    user: {
      unsafeMetadata: overrides.unsafeMetadata ?? {},
      update: overrides.userUpdate ?? vi.fn().mockResolvedValue(undefined),
    },
  }),
  useOrganization: () => ({ organization: null, isLoaded: true }),
  useOrganizationList: () => ({
    userMemberships: { data: overrides.memberships ?? [] },
    userInvitations: {
      data: overrides.invitations ?? [],
      revalidate: vi.fn(),
    },
    isLoaded: true,
    setActive: vi.fn(),
  }),
  CreateOrganization: () => <div>CreateOrganization mock</div>,
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
  useSearchParams: () => new URLSearchParams(searchString),
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ syncUser: syncUserMock }) }));
// lib/analytics re-exports PostHogProvider at module load; the mock must
// expose it (vitest is strict about import-time destructuring vs mocks).
vi.mock("posthog-js/react", () => ({
  usePostHog: () => null,
  PostHogProvider: ({ children }: { children: React.ReactNode }) => children,
}));

describe("OnboardingPage", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.doUnmock("@clerk/nextjs");
    searchString = "";
    syncUserMock.mockClear();
    pushMock.mockClear();
  });

  it("forces invitation mode when pending invitations exist — no Skip button", async () => {
    vi.doMock("@clerk/nextjs", () =>
      buildClerkMocks({
        invitations: [
          {
            id: "orginv_1",
            publicOrganizationData: { name: "Acme Org" },
            accept: vi.fn(),
          },
        ],
      }),
    );
    const { default: Page } = await import("../page");
    render(<Page />);
    expect(screen.getByText("Acme Org")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /skip invitations/i }),
    ).toBeNull();
  });

  it("auto-completes personal onboarding when no invitations and no team intent", async () => {
    const userUpdate = vi.fn().mockResolvedValue(undefined);
    // Seed a pre-existing unsafeMetadata key so the assertion below actually
    // exercises the merge-preserving write (a replace-only regression would
    // drop providerChoice and fail). This invariant is what keeps the
    // deferred provider fix safe.
    vi.doMock("@clerk/nextjs", () =>
      buildClerkMocks({ userUpdate, unsafeMetadata: { providerChoice: "byo_key" } }),
    );
    const { default: Page } = await import("../page");
    render(<Page />);
    // No chooser is shown any more.
    expect(screen.queryByRole("button", { name: /personal/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /organization/i })).toBeNull();
    // Personal path runs automatically: onboarded flag is added WITHOUT
    // clobbering the existing providerChoice key, then syncUser + redirect.
    await waitFor(() =>
      expect(userUpdate).toHaveBeenCalledWith({
        unsafeMetadata: { providerChoice: "byo_key", onboarded: true },
      }),
    );
    await waitFor(() => expect(syncUserMock).toHaveBeenCalled());
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/chat"));
  });

  it("renders CreateOrganization when intent=team", async () => {
    searchString = "intent=team";
    vi.doMock("@clerk/nextjs", () => buildClerkMocks());
    const { default: Page } = await import("../page");
    render(<Page />);
    expect(screen.getByText("CreateOrganization mock")).toBeInTheDocument();
    // Personal auto-complete must NOT fire on the team path.
    expect(syncUserMock).not.toHaveBeenCalled();
  });
});
