// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import ConnectMachinePage from "../page";

const s = vi.hoisted(() => ({
  signedIn: true,
  organization: { id: "org_a", name: "Acme" } as { id: string; name: string } | null,
  post: vi.fn(),
  signInProps: vi.fn(),
  query: "code=ABCD-EFGH",
}));
vi.mock("@/lib/api", async () => {
  const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
  return { ApiError, useApi: () => ({ post: s.post }) };
});
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(s.query) }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: s.signedIn }),
  useOrganization: () => ({ isLoaded: true, organization: s.organization }),
  SignIn: (props: unknown) => { s.signInProps(props); return <div>sign in</div>; },
  OrganizationSwitcher: () => <div>switch workspace</div>,
}));

beforeEach(() => {
  Object.assign(s, { signedIn: true, organization: { id: "org_a", name: "Acme" }, query: "code=ABCD-EFGH" });
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
      screen.getByText(/Only continue if you just ran `lightbulb join` on this Mac and the code matches/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Connect this Mac" }));
    expect(await screen.findByText("Connected. You can close this tab.")).toBeInTheDocument();
    expect(s.post).toHaveBeenCalledWith("/auth/device/approve", { userCode: "ABCD-EFGH" });
  });

  it("an expired code reads plainly", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    s.post.mockRejectedValue(new ApiError(410, { detail: "expired" }));
    render(<ConnectMachinePage />);
    expect(await screen.findByText("This code expired. Run `lightbulb join` again.")).toBeInTheDocument();
  });

  it("with no workspace selected asks for one and offers no button", () => {
    s.organization = null;
    render(<ConnectMachinePage />);
    expect(screen.getByText("switch workspace")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect this Mac" })).not.toBeInTheDocument();
  });
});

describe("sign in a Lightbulb Connect computer (mode=network)", () => {
  const COPY = "Sign in to verify your name on the Lightbulb network. It confirms who you are and nothing more.";

  it("signed out shows Clerk sign-in that returns to network mode", () => {
    Object.assign(s, { signedIn: false, query: "mode=network" });
    render(<ConnectMachinePage />);
    expect(s.signInProps).toHaveBeenCalledWith(
      expect.objectContaining({ forceRedirectUrl: "/auth/connect-machine?mode=network" }),
    );
  });

  it("the person types the code, sees the computer and key short, then signs it in with no workspace", async () => {
    Object.assign(s, { organization: null, query: "mode=network&code=EVIL-CODE" });
    s.post.mockImplementation(async (url: string) =>
      url === "/network/device/describe" ? { computer_name: "Ada's MacBook", key_short: "abcdefghijkl" } : { status: "approved" },
    );
    const { container } = render(<ConnectMachinePage />);
    const seen: string[] = [];
    const look = () => seen.push(container.textContent ?? "");
    expect(screen.getByText(COPY)).toBeInTheDocument();
    look();
    const box = screen.getByLabelText("Enter the code your computer shows") as HTMLInputElement;
    expect(box.value).toBe(""); // nothing in the URL fills it in
    fireEvent.change(box, { target: { value: " WXYZ-2345 " } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("Sign in Ada's MacBook?")).toBeInTheDocument();
    expect(screen.getByText("abcdefghijkl")).toBeInTheDocument();
    look();
    expect(s.post).toHaveBeenCalledWith("/network/device/describe", { user_code: "WXYZ-2345" });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Done. You can close this tab.")).toBeInTheDocument();
    expect(s.post).toHaveBeenCalledWith("/network/device/approve", { user_code: "WXYZ-2345" });
    look();
    expect(seen.join(" ")).not.toMatch(/workspace|plan|trial|upgrade|price|\$/i); // the free path
  });

  it("a wrong or expired code reads plainly", async () => {
    const { ApiError } = await vi.importActual<typeof import("@lightbulb/shared")>("@lightbulb/shared");
    s.query = "mode=network";
    s.post.mockRejectedValue(new ApiError(410, { detail: "expired" }));
    render(<ConnectMachinePage />);
    fireEvent.change(screen.getByLabelText("Enter the code your computer shows"), { target: { value: "AAAA-BBBB" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("This code is wrong or expired. Start again on your computer.")).toBeInTheDocument();
  });
});
