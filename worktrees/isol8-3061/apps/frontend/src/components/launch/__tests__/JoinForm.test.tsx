import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn(), post, put: vi.fn(), patch: vi.fn(), del: vi.fn() }), ApiError: class extends Error { constructor(public status: number, public body: unknown, message?: string) { super(message); } } }));
const capture = vi.fn();
vi.mock("@/lib/analytics", () => ({ capture: (...a: unknown[]) => capture(...a) }));

import { JoinForm } from "../JoinForm";

describe("JoinForm", () => {
  beforeEach(() => { post.mockReset(); capture.mockReset(); });

  it("labels the display name as shown on the public board, and starts empty with a placeholder when there's no suggested name", () => {
    render(<JoinForm onJoined={vi.fn()} />);
    expect(screen.getByText(/shown on the public board/i)).toBeInTheDocument();
    const input = screen.getByLabelText(/display name/i);
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("placeholder", "Your company name");
  });

  it("prefills from suggestedName, and an edit still submits what the user typed", async () => {
    post.mockResolvedValue({ display_name: "Ada L", public: true });
    render(<JoinForm onJoined={vi.fn()} suggestedName="Northwind Ceramics" />);
    const input = screen.getByLabelText(/display name/i);
    expect(input).toHaveValue("Northwind Ceramics");
    fireEvent.change(input, { target: { value: "Ada L" } });
    fireEvent.click(screen.getByRole("button", { name: /join the competition/i }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/join", { display_name: "Ada L", public: true }));
  });

  it("posts display_name and public, captures analytics, and calls onJoined", async () => {
    const onJoined = vi.fn();
    post.mockResolvedValue({ display_name: "Ada", public: true });
    render(<JoinForm onJoined={onJoined} />);
    fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: "Ada" } });
    fireEvent.click(screen.getByRole("button", { name: /join the competition/i }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/join", { display_name: "Ada", public: true }));
    expect(capture).toHaveBeenCalledWith("launch_joined", { public: true });
    expect(onJoined).toHaveBeenCalled();
  });

  it("unchecking the public box posts public: false", async () => {
    post.mockResolvedValue({ display_name: "Ada", public: false });
    render(<JoinForm onJoined={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: "Ada" } });
    fireEvent.click(screen.getByLabelText(/show me on the public board/i));
    fireEvent.click(screen.getByRole("button", { name: /join the competition/i }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/join", { display_name: "Ada", public: false }));
  });

  it("maps a 409 {reason} refusal to copy", async () => {
    const { ApiError } = await import("@/lib/api");
    post.mockRejectedValue(new ApiError(409, { detail: { reason: "not_onboarded" } }, "API 409"));
    render(<JoinForm onJoined={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: "Ada" } });
    fireEvent.click(screen.getByRole("button", { name: /join the competition/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Finish onboarding first/);
  });

  it("tells an org actor to switch to their personal workspace", async () => {
    const { ApiError } = await import("@/lib/api");
    post.mockRejectedValue(new ApiError(409, { detail: { reason: "org_not_supported" } }, "API 409"));
    render(<JoinForm onJoined={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: "Ada" } });
    fireEvent.click(screen.getByRole("button", { name: /join the competition/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Switch to your personal workspace/);
  });

  it("maps a plain-string 400 detail to copy", async () => {
    const { ApiError } = await import("@/lib/api");
    post.mockRejectedValue(new ApiError(400, { detail: "no_email" }, "API 400"));
    render(<JoinForm onJoined={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: "Ada" } });
    fireEvent.click(screen.getByRole("button", { name: /join the competition/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't find an email/i);
  });
});
