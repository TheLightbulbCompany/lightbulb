import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const patch = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch, del: vi.fn() }),
  ApiError: class extends Error {
    constructor(public status: number, public body: unknown, message?: string) { super(message); }
  },
}));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { RenameForm } from "../RenameForm";

describe("RenameForm", () => {
  beforeEach(() => patch.mockReset());

  it("stays collapsed, showing the current board name", () => {
    render(<RenameForm current="Northwind Ceramics" onRenamed={vi.fn()} />);
    expect(screen.getByText("Northwind Ceramics")).toBeInTheDocument();
    expect(screen.queryByLabelText(/business name/i)).not.toBeInTheDocument();
  });

  it("renames, reports the new name up, and collapses again", async () => {
    const onRenamed = vi.fn();
    patch.mockResolvedValue({ display_name: "Northwind Studio" });
    render(<RenameForm current="Northwind Ceramics" onRenamed={onRenamed} />);
    fireEvent.click(screen.getByRole("button", { name: /change/i }));
    fireEvent.change(screen.getByLabelText(/business name/i), { target: { value: "  Northwind Studio  " } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() =>
      // Trimmed before it leaves the client, matching the join path.
      expect(patch).toHaveBeenCalledWith("/launch/display-name", { display_name: "Northwind Studio" }),
    );
    await waitFor(() => expect(onRenamed).toHaveBeenCalledWith("Northwind Studio"));
    await waitFor(() => expect(screen.queryByLabelText(/business name/i)).not.toBeInTheDocument());
  });

  it("keeps Save disabled below the 2-character floor the backend enforces", () => {
    render(<RenameForm current="Northwind Ceramics" onRenamed={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /change/i }));
    const input = screen.getByLabelText(/business name/i);
    fireEvent.change(input, { target: { value: " a " } });
    expect(screen.getByRole("button", { name: /^save$/i })).toBeDisabled();
    fireEvent.change(input, { target: { value: "Ab" } });
    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });

  it("surfaces a failure and keeps the form open with the typed name", async () => {
    patch.mockRejectedValue(new Error("boom"));
    render(<RenameForm current="Northwind Ceramics" onRenamed={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /change/i }));
    fireEvent.change(screen.getByLabelText(/business name/i), { target: { value: "Northwind Studio" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Couldn't rename/);
    expect(screen.getByLabelText(/business name/i)).toHaveValue("Northwind Studio");
  });

  it("Cancel restores the current name and drops the edit", () => {
    render(<RenameForm current="Northwind Ceramics" onRenamed={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /change/i }));
    fireEvent.change(screen.getByLabelText(/business name/i), { target: { value: "Typo Co" } });
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.getByText("Northwind Ceramics")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /change/i }));
    expect(screen.getByLabelText(/business name/i)).toHaveValue("Northwind Ceramics");
  });

  it("names the fallback when the owner has no stored name yet", () => {
    render(<RenameForm current={null} onRenamed={vi.fn()} />);
    expect(screen.getByText(/your business/i)).toBeInTheDocument();
  });
});
