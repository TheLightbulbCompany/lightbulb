import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const update = vi.fn().mockResolvedValue(undefined);
vi.mock("@clerk/nextjs", () => ({
  useUser: () => ({
    user: { unsafeMetadata: {}, update },
    isLoaded: true,
  }),
}));

import { FirstRunFunnel } from "@/components/onboarding/FirstRunFunnel";

beforeEach(() => {
  post.mockReset();
  update.mockReset().mockResolvedValue(undefined);
});

describe("FirstRunFunnel", () => {
  it("walks grow path: choose -> enter url -> submit -> writes metadata", async () => {
    post.mockResolvedValue({ status: "staged", friday_agent_id: "f1" });
    render(<FirstRunFunnel />);

    fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
    fireEvent.change(screen.getByPlaceholderText(/yourcompany\.com/i), {
      target: { value: "acme.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/onboarding/brief", {
        path: "grow",
        input: "acme.com",
        detail: undefined,
      });
    });
    await waitFor(() => {
      const meta = update.mock.calls[0][0].unsafeMetadata.firstRun;
      expect(meta.briefSubmitted).toBe(true);
      expect(meta.path).toBe("grow");
    });
  });

  it("skip writes skipped flag without posting", async () => {
    render(<FirstRunFunnel />);
    fireEvent.click(screen.getByRole("button", { name: /start something new/i }));
    fireEvent.click(screen.getByRole("button", { name: /skip for now/i }));
    await waitFor(() => {
      expect(update.mock.calls[0][0].unsafeMetadata.firstRun.skipped).toBe(true);
    });
    expect(post).not.toHaveBeenCalled();
  });
});
