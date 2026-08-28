import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn(), post, put: vi.fn(), del: vi.fn() }), ApiError: class extends Error { constructor(public status: number, public body: unknown, message?: string) { super(message); } } }));
const capture = vi.fn();
vi.mock("@/lib/analytics", () => ({ capture: (...a: unknown[]) => capture(...a) }));

import { EnterForm } from "../EnterForm";

function fill(name: string, key: string) {
  fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: name } });
  fireEvent.change(screen.getByLabelText(/restricted key/i), { target: { value: key } });
}

describe("EnterForm", () => {
  beforeEach(() => { post.mockReset(); capture.mockReset(); });

  it("refuses a secret key client-side without calling the API", async () => {
    render(<EnterForm onEntered={vi.fn()} />);
    fill("Ada", "sk_live_" + "x".repeat(24));
    fireEvent.click(screen.getByRole("button", { name: /enter/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/restricted key/i);
    expect(post).not.toHaveBeenCalled();
  });

  it("posts and reports success without leaking the key to analytics", async () => {
    const onEntered = vi.fn();
    post.mockResolvedValue({ display_name: "Ada" });
    render(<EnterForm onEntered={onEntered} />);
    const key = "rk_live_" + "x".repeat(24);
    fill("Ada", key);
    fireEvent.click(screen.getByRole("button", { name: /enter/i }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/launch/enter", { display_name: "Ada", stripe_key: key, public: true }));
    expect(onEntered).toHaveBeenCalled();
    expect(JSON.stringify(capture.mock.calls)).not.toContain(key);
  });

  it("maps a backend reason to copy", async () => {
    const { ApiError } = await import("@/lib/api");
    post.mockRejectedValue(new ApiError(400, { detail: "stripe_permission" }, "stripe_permission"));
    render(<EnterForm onEntered={vi.fn()} />);
    fill("Ada", "rk_live_" + "x".repeat(24));
    fireEvent.click(screen.getByRole("button", { name: /enter/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Balance: Read/);
  });
});
