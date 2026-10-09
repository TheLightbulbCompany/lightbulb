import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import DeploySuccessPage from "../page";

const { stableAuth } = vi.hoisted(() => ({
  stableAuth: { isLoaded: true, isSignedIn: true, getToken: async () => "test-jwt" },
}));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => stableAuth }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("listing_id=lst_1&session_id=cs_1"),
}));
vi.mock("@/lib/marketplace/api", () => ({
  getListing: vi.fn().mockRejectedValue(new Error("offline")),
}));

describe("deploy-success", () => {
  it("finishes the deploy it started instead of hanging on Hiring…, and links stay on this host", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ agent_uuid: "agent-9" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<DeploySuccessPage />);

    await waitFor(() => expect(screen.getByText(/You hired/)).toBeTruthy());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const link = screen.getByRole("link", { name: /Open chat/ });
    expect(link.getAttribute("href")).toBe("/chat?agent=agent-9");
    vi.unstubAllGlobals();
  });
});
