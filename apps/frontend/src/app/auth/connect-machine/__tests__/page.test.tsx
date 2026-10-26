// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import ConnectMachinePage from "../page";

const s = vi.hoisted(() => ({
  signedIn: true,
  organization: { id: "org_a", name: "Acme" } as { id: string; name: string } | null,
  post: vi.fn(),
  signInProps: vi.fn(),
}));
vi.mock("@/lib/api", async () => {
  const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
  return { ApiError, useApi: () => ({ post: s.post }) };
});
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams("code=ABCD-EFGH") }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: s.signedIn }),
  useOrganization: () => ({ isLoaded: true, organization: s.organization }),
  SignIn: (props: unknown) => { s.signInProps(props); return <div>sign in</div>; },
  OrganizationSwitcher: () => <div>switch workspace</div>,
}));

beforeEach(() => {
  Object.assign(s, { signedIn: true, organization: { id: "org_a", name: "Acme" } });
  vi.clearAllMocks();
});

describe("connect this Mac", () => {
  it("signed out shows Clerk sign-in that returns here with the code", () => {
    s.signedIn = false;
    render(<ConnectMachinePage />);
    expect(screen.getByText("sign in")).toBeInTheDocument();
    expect(s.signInProps).toHaveBeenCalledWith(
      expect.objectContaining({ forceRedirectUrl: "/auth/connect-machine?code=ABCD-EFGH" }),
    );
  });

  it("signed in shows the machine name and the warning, then approves", async () => {
    s.post.mockImplementation(async (url: string) =>
      url === "/auth/device/describe" ? { machineName: "Ada's MacBook" } : { status: "approved" },
    );
    render(<ConnectMachinePage />);
    expect(await screen.findByText("Connect Ada's MacBook to Acme?")).toBeInTheDocument();
    expect(screen.getByText("ABCD-EFGH")).toBeInTheDocument();
    expect(
      screen.getByText(/Only continue if you just ran `lightbulb pair` on this Mac and the code matches/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Connect this Mac" }));
    expect(await screen.findByText("Connected. You can close this tab.")).toBeInTheDocument();
    expect(s.post).toHaveBeenCalledWith("/auth/device/approve", { userCode: "ABCD-EFGH" });
  });

  it("an expired code reads plainly", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    s.post.mockRejectedValue(new ApiError(410, { detail: "expired" }));
    render(<ConnectMachinePage />);
    expect(await screen.findByText("This code expired. Run `lightbulb pair` again.")).toBeInTheDocument();
  });

  it("with no workspace selected asks for one and offers no button", () => {
    s.organization = null;
    render(<ConnectMachinePage />);
    expect(screen.getByText("switch workspace")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect this Mac" })).not.toBeInTheDocument();
  });
});
