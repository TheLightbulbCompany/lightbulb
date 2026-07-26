import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

const pushMock = vi.fn();
const replaceMock = vi.fn();
let searchString = "";

// Minimal Clerk mocks — configurable invitations / memberships / org.
const buildClerkMocks = (
  overrides: {
    invitations?: unknown[];
    memberships?: unknown[];
  } = {},
) => ({
  useAuth: () => ({ isLoaded: true, orgId: null }),
  useUser: () => ({
    user: {
      unsafeMetadata: {},
      update: vi.fn().mockResolvedValue(undefined),
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
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  useSearchParams: () => new URLSearchParams(searchString),
}));
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
    pushMock.mockClear();
    replaceMock.mockClear();
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
    // Not redirected away — the invitee stays here to accept.
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("renders CreateOrganization when intent=team", async () => {
    searchString = "intent=team";
    vi.doMock("@clerk/nextjs", () => buildClerkMocks());
    const { default: Page } = await import("../page");
    render(<Page />);
    expect(screen.getByText("CreateOrganization mock")).toBeInTheDocument();
    // Team creation stays on this page; no bounce to /chat.
    expect(replaceMock).not.toHaveBeenCalledWith("/chat");
  });

  it("redirects a stray visitor (no team intent, no invitation) to /chat", async () => {
    // Personal signups are owned by useOnboardingGate and never routed here;
    // anyone who lands with no team/invite context is sent on to /chat. There
    // is no personal chooser or auto-complete on this page any more.
    vi.doMock("@clerk/nextjs", () => buildClerkMocks());
    const { default: Page } = await import("../page");
    render(<Page />);
    expect(screen.queryByRole("button", { name: /personal/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /organization/i })).toBeNull();
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/chat"));
  });
});
