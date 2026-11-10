// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { vi, it, expect } from "vitest";

const api = vi.hoisted(() => ({
  get: vi.fn(async () => ({ set: true, mobile: "+14155550133" })),
  put: vi.fn(async (_p: string, body: { mobile: string }) => ({ set: true, mobile: body.mobile })),
}));
vi.mock("@/lib/api", () => ({ useApi: () => api }));

import { PhonePinCard } from "../PhonePinCard";

it("sets or changes the PIN and never shows it back", async () => {
  render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}><PhonePinCard /></SWRConfig>);
  expect(await screen.findByText(/A PIN is set/)).toBeInTheDocument();
  expect(screen.getByLabelText("Your mobile number")).toHaveValue("+14155550133");
  const pin = screen.getByLabelText(/New PIN/);
  expect(pin).toHaveValue("");
  expect(pin).toHaveAttribute("type", "password");
  fireEvent.change(pin, { target: { value: "48a21" } });
  expect(pin).toHaveValue("4821");
  fireEvent.click(screen.getByRole("button", { name: "Change PIN" }));
  await waitFor(() => expect(api.put).toHaveBeenCalledWith("/voice/phone-pin", { mobile: "+14155550133", pin: "4821" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
  expect(pin).toHaveValue("");
});
