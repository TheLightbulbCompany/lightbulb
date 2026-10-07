import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn(), post, put: vi.fn(), patch: vi.fn(), del: vi.fn() }), ApiError: class extends Error { constructor(public status: number, public body: unknown, message?: string) { super(message); } } }));
const capture = vi.fn();
vi.mock("@/lib/analytics", () => ({ capture: (...a: unknown[]) => capture(...a) }));

import { ConnectStripeForm } from "../ConnectStripeForm";

function fillKey(key: string) {
  fireEvent.change(screen.getByLabelText(/restricted key/i), { target: { value: key } });
}

describe("ConnectStripeForm", () => {
  beforeEach(() => { post.mockReset(); capture.mockReset(); });

  it("refuses a secret key client-side without calling the API", async () => {
    render(<ConnectStripeForm onConnected={vi.fn()} />);
    fillKey("sk_live_" + "x".repeat(24));
    fireEvent.click(screen.getByRole("button", { name: /connect/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/restricted key/i);
    expect(post).not.toHaveBeenCalled();
  });

  it("posts only the key (no display_name/public) and reports success without leaking the key to analytics", async () => {
    const onConnected = vi.fn();
    post.mockResolvedValue({ has_key: true });
    render(<ConnectStripeForm onConnected={onConnected} />);
    const key = "rk_live_" + "x".repeat(24);
    fillKey(key);
    fireEvent.click(screen.getByRole("button", { name: /connect/i }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/connect-stripe", { stripe_key: key }));
    expect(onConnected).toHaveBeenCalled();
    expect(JSON.stringify(capture.mock.calls)).not.toContain(key);
  });

  it("maps a backend reason to copy", async () => {
    const { ApiError } = await import("@/lib/api");
    post.mockRejectedValue(new ApiError(400, { detail: "stripe_permission" }, "stripe_permission"));
    render(<ConnectStripeForm onConnected={vi.fn()} />);
    fillKey("rk_live_" + "x".repeat(24));
    fireEvent.click(screen.getByRole("button", { name: /connect/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Balance: Read/);
  });

  it("maps the no_entry reason to copy", async () => {
    const { ApiError } = await import("@/lib/api");
    post.mockRejectedValue(new ApiError(400, { detail: "no_entry" }, "no_entry"));
    render(<ConnectStripeForm onConnected={vi.fn()} />);
    fillKey("rk_live_" + "x".repeat(24));
    fireEvent.click(screen.getByRole("button", { name: /connect/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Join the competition first/);
  });
});
