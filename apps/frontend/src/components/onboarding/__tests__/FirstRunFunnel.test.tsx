import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

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

// RETRY_MS / MAX_RETRIES mirrored from FirstRunFunnel.tsx (not exported).
const RETRY_MS = 3000;
const MAX_RETRIES = 5;

function enterGrowPathAndSubmit() {
  fireEvent.click(screen.getByRole("button", { name: /grow my business/i }));
  fireEvent.change(screen.getByPlaceholderText(/yourcompany\.com/i), {
    target: { value: "acme.com" },
  });
  fireEvent.click(screen.getByRole("button", { name: /continue/i }));
}

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

  it("does not write briefSubmitted when the response body is not staged/already_submitted", async () => {
    // Regression: the backend's `_ctx` dependency can return HTTP 202 while
    // Paperclip is still provisioning. The api client treats any 2xx as a
    // resolved promise, so the funnel must check the BODY, not just that
    // the POST resolved -- otherwise it writes briefSubmitted:true with
    // nothing staged, permanently bricking the show.
    vi.useFakeTimers();
    try {
      post.mockResolvedValue({ status: "provisioning" });
      render(<FirstRunFunnel />);
      enterGrowPathAndSubmit();

      await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));
      await vi.waitFor(() =>
        expect(screen.getByRole("button", { name: /setting things up/i })).toBeInTheDocument(),
      );
      expect(update).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("retries an unexpected response body exactly like a 409, then lands in the error state", async () => {
    vi.useFakeTimers();
    try {
      post.mockResolvedValue({ detail: "still working" });
      render(<FirstRunFunnel />);
      enterGrowPathAndSubmit();

      await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(1));

      // 5 retries at RETRY_MS apart before the funnel gives up on attempt
      // MAX_RETRIES (6 total POSTs).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(MAX_RETRIES * RETRY_MS + 1000);
      });

      await vi.waitFor(() => expect(post).toHaveBeenCalledTimes(MAX_RETRIES + 1));
      await vi.waitFor(() =>
        expect(screen.getByText(/couldn.t save that just yet/i)).toBeInTheDocument(),
      );
      expect(update).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("offers no skip — the funnel is the committed onboarding front door", () => {
    render(<FirstRunFunnel />);
    // Path-picker screen: no skip.
    expect(screen.queryByRole("button", { name: /skip/i })).toBeNull();
    // ...and none on the input screen either.
    fireEvent.click(screen.getByRole("button", { name: /start something new/i }));
    expect(screen.queryByRole("button", { name: /skip/i })).toBeNull();
  });
});
