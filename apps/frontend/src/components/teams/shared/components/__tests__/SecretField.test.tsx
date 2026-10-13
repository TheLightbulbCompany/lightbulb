import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { SecretField } from "../SecretField";
import { notifyApiError } from "@/lib/notify";

vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));

const writeText = vi.fn();
beforeEach(() => {
  writeText.mockReset();
  vi.mocked(notifyApiError).mockReset();
  Object.assign(navigator, { clipboard: { writeText } });
});

describe("<SecretField>", () => {
  it("shows Copied after a successful copy", async () => {
    writeText.mockResolvedValue(undefined);
    render(<SecretField label="Secret" value="whsec_1" />);
    fireEvent.click(screen.getByRole("button", { name: /copy secret/i }));
    expect(await screen.findByRole("status")).toHaveTextContent("Copied");
    expect(writeText).toHaveBeenCalledWith("whsec_1");
  });

  it("notifies when the clipboard write fails", async () => {
    writeText.mockRejectedValue(new Error("denied"));
    render(<SecretField label="Secret" value="whsec_1" />);
    fireEvent.click(screen.getByRole("button", { name: /copy secret/i }));
    await vi.waitFor(() => expect(notifyApiError).toHaveBeenCalled());
    expect(screen.queryByRole("status")).toBeNull();
  });
});
