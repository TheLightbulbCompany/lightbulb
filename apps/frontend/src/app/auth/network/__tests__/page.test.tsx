// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import NetworkConfirmPage from "../page";

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

function describes(purpose: "login" | "agents") {
  s.post.mockImplementation(async (url: string) =>
    url === "/network/device/describe" ? { purpose, computer: "Ada's Mac" } : { status: "approved" },
  );
}

beforeEach(() => {
  Object.assign(s, { signedIn: true, organization: { id: "org_a", name: "Acme" } });
  vi.clearAllMocks();
});

describe("confirm a lightbulb computer", () => {
  it("signed out shows Clerk sign-in that returns here with the code", () => {
    s.signedIn = false;
    render(<NetworkConfirmPage />);
    expect(screen.getByText("sign in")).toBeInTheDocument();
    expect(s.signInProps).toHaveBeenCalledWith(
      expect.objectContaining({ forceRedirectUrl: "/auth/network?code=ABCD-EFGH" }),
    );
  });

  it("login names the computer, shows the code, and confirms", async () => {
    describes("login");
    render(<NetworkConfirmPage />);
    expect(await screen.findByText("Sign in Ada's Mac as you?")).toBeInTheDocument();
    expect(screen.getByText("ABCD-EFGH")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Sign in this computer" }));
    expect(await screen.findByText("This computer is signed in. You can close this tab.")).toBeInTheDocument();
    expect(s.post).toHaveBeenCalledWith("/network/device/approve", { userCode: "ABCD-EFGH" });
  });

  it("login needs no workspace", async () => {
    s.organization = null;
    describes("login");
    render(<NetworkConfirmPage />);
    expect(await screen.findByRole("button", { name: "Sign in this computer" })).toBeInTheDocument();
  });

  it("agents asks about the selected workspace and says to allow it after", async () => {
    describes("agents");
    render(<NetworkConfirmPage />);
    expect(await screen.findByText("Add Acme's agents to Ada's Mac's network?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add the agents" }));
    expect(await screen.findByText("Done. Allow Acme on Ada's Mac when it asks.")).toBeInTheDocument();
  });

  it("agents with no workspace selected asks for one and offers no button", async () => {
    s.organization = null;
    describes("agents");
    render(<NetworkConfirmPage />);
    expect(await screen.findByText("switch workspace")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add the agents" })).not.toBeInTheDocument();
  });

  it("refusals read plainly", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    s.post.mockImplementation(async (url: string) => {
      if (url === "/network/device/describe") return { purpose: "agents", computer: "Ada's Mac" };
      throw new ApiError(409, { detail: "Sign this computer in as you first." });
    });
    render(<NetworkConfirmPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Add the agents" }));
    expect(await screen.findByText("Sign this computer in as you first.")).toBeInTheDocument();

    s.post.mockRejectedValue(new ApiError(410, { detail: "expired" }));
    render(<NetworkConfirmPage />);
    expect(await screen.findByText("This code expired. Run the command again.")).toBeInTheDocument();
  });
});
