// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
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
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: s.signedIn }),
  useOrganization: () => ({ isLoaded: true, organization: s.organization }),
  SignIn: (props: unknown) => { s.signInProps(props); return <div>sign in</div>; },
  OrganizationSwitcher: () => <div>switch workspace</div>,
}));

function describes(purpose: "login" | "agents") {
  s.post.mockImplementation(async (url: string) =>
    url === "/network/device/describe"
      ? { purpose, computer: "Ada's Mac", startedAt: Math.round(Date.now() / 1000) - 5, ip: "203.0.113.7" }
      : { status: "approved" },
  );
}

function typeCode(code = "abcd-efgh") {
  fireEvent.change(screen.getByLabelText("Code"), { target: { value: code } });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
}

beforeEach(() => {
  Object.assign(s, { signedIn: true, organization: { id: "org_a", name: "Acme" } });
  vi.clearAllMocks();
});

describe("confirm a lightbulb computer", () => {
  it("signed out shows Clerk sign-in (and sign-up) that return here", () => {
    s.signedIn = false;
    render(<NetworkConfirmPage />);
    expect(screen.getByText("sign in")).toBeInTheDocument();
    expect(s.signInProps).toHaveBeenCalledWith(
      expect.objectContaining({ forceRedirectUrl: "/auth/network", signUpForceRedirectUrl: "/auth/network" }),
    );
  });

  it("the person types the code; the page names the computer, when and where, then confirms", async () => {
    describes("login");
    render(<NetworkConfirmPage />);
    expect(s.post).not.toHaveBeenCalled(); // nothing is looked up before a code is typed
    typeCode();
    expect(await screen.findByText("Sign in Ada's Mac as you?")).toBeInTheDocument();
    expect(s.post).toHaveBeenCalledWith("/network/device/describe", { userCode: "ABCD-EFGH" });
    expect(screen.getByText(/Requested 5 seconds ago from 203\.0\.113\.7/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Sign in this computer" }));
    expect(await screen.findByText("This computer is signed in. You can close this tab.")).toBeInTheDocument();
    expect(s.post).toHaveBeenCalledWith("/network/device/approve", { userCode: "ABCD-EFGH" });
  });

  it("login needs no workspace", async () => {
    s.organization = null;
    describes("login");
    render(<NetworkConfirmPage />);
    typeCode();
    expect(await screen.findByRole("button", { name: "Sign in this computer" })).toBeInTheDocument();
  });

  it("agents asks about the selected workspace and says to allow it after", async () => {
    describes("agents");
    render(<NetworkConfirmPage />);
    typeCode();
    expect(await screen.findByText("Add Acme's agents to Ada's Mac's network?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add the agents" }));
    expect(await screen.findByText("Done. Allow Acme on Ada's Mac when it asks.")).toBeInTheDocument();
  });

  it("agents with no workspace selected asks for one and offers no button", async () => {
    s.organization = null;
    describes("agents");
    render(<NetworkConfirmPage />);
    typeCode();
    expect(await screen.findByText("switch workspace")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add the agents" })).not.toBeInTheDocument();
  });

  it("a starting workspace (504) is asked again with the same code", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    let tries = 0;
    s.post.mockImplementation(async (url: string) => {
      if (url === "/network/device/describe") return { purpose: "agents", computer: "Ada's Mac" };
      if (++tries === 1) throw new ApiError(504, null);
      return { status: "approved" };
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<NetworkConfirmPage />);
      typeCode();
      fireEvent.click(await screen.findByRole("button", { name: "Add the agents" }));
      await act(async () => { await vi.advanceTimersByTimeAsync(16000); });
      expect(await screen.findByText("Done. Allow Acme on Ada's Mac when it asks.")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
    expect(tries).toBe(2);
  });

  it("refusals read plainly", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    s.post.mockImplementation(async (url: string) => {
      if (url === "/network/device/describe") return { purpose: "agents", computer: "Ada's Mac" };
      throw new ApiError(409, { detail: "Sign this computer in as you first." });
    });
    render(<NetworkConfirmPage />);
    typeCode();
    fireEvent.click(await screen.findByRole("button", { name: "Add the agents" }));
    expect(await screen.findByText("Sign this computer in as you first.")).toBeInTheDocument();
  });

  it("an unknown or expired code says so", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    s.post.mockRejectedValue(new ApiError(410, { detail: "expired" }));
    render(<NetworkConfirmPage />);
    typeCode();
    expect(await screen.findByText(/No such code, or it expired/)).toBeInTheDocument();
  });
});
