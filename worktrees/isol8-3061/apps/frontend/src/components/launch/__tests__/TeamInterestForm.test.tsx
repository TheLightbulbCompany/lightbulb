import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const post = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: vi.fn(), post, put: vi.fn(), patch: vi.fn(), del: vi.fn() }),
  ApiError: class extends Error {
    constructor(
      public status: number,
      public body: unknown,
      message?: string,
    ) {
      super(message);
    }
  },
}));
const capture = vi.fn();
vi.mock("@/lib/analytics", () => ({ capture: (...a: unknown[]) => capture(...a) }));

import { TeamInterestForm } from "../TeamInterestForm";

describe("TeamInterestForm", () => {
  beforeEach(() => {
    post.mockReset();
    capture.mockReset();
  });

  it("asks a signed-out visitor to sign in, and offers no form", () => {
    render(<TeamInterestForm signedIn={false} />);
    expect(screen.getByText(/sign in first/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /tell the founders/i })).not.toBeInTheDocument();
  });

  it("posts the team details and confirms WITHOUT implying an entry", async () => {
    post.mockResolvedValue({ team_name: "Tuple", seats: 4, note: "", submitted_at: "t", counted: false });
    render(<TeamInterestForm signedIn />);
    fireEvent.change(screen.getByLabelText(/team or company/i), { target: { value: " Tuple " } });
    fireEvent.change(screen.getByLabelText(/^seats$/i), { target: { value: "4" } });
    fireEvent.change(screen.getByLabelText(/anything we should know/i), { target: { value: "six of us" } });
    fireEvent.click(screen.getByRole("button", { name: /tell the founders/i }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/launch/team-interest", {
        team_name: "Tuple",
        seats: 4,
        note: "six of us",
      }),
    );
    expect(capture).toHaveBeenCalledWith("launch_team_interest", { seats: 4 });
    // The load-bearing copy: a founder gets in touch, nothing is entered yet.
    const done = await screen.findByRole("status");
    expect(done).toHaveTextContent(/founder will email you/i);
    expect(done).toHaveTextContent(/nothing is entered yet/i);
  });

  it("says the form itself only sends details, never enters the team", () => {
    render(<TeamInterestForm signedIn />);
    expect(screen.getByText(/doesn't enter you/i)).toBeInTheDocument();
  });

  it("surfaces a rate limit", async () => {
    const { ApiError } = await import("@/lib/api");
    post.mockRejectedValue(new ApiError(429, {}, "API 429"));
    render(<TeamInterestForm signedIn />);
    fireEvent.change(screen.getByLabelText(/team or company/i), { target: { value: "Tuple" } });
    fireEvent.change(screen.getByLabelText(/^seats$/i), { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: /tell the founders/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/too many tries/i);
  });
});
