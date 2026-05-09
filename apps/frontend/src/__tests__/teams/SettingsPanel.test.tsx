// Test for SettingsPanel — confirms PATCH body contains ONLY
// {displayName, description}. Adapter/plugin/instance fields would 422
// at the BFF (PatchCompanySettingsBody, extra="forbid").
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const mockPatch = vi.fn();
const mockMutate = vi.fn();
// Use a stable object reference: useEffect depends on `data` identity, so
// returning a new object on every render would re-run the effect and fight
// with user-typed state.
const stableData = { displayName: "Acme Co", description: "Building things" };
const stableResp = {
  data: stableData,
  isLoading: false,
  error: null,
  mutate: mockMutate,
};
const mockRead = vi.fn(() => stableResp);

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: vi.fn(),
    patch: mockPatch,
    del: vi.fn(),
  }),
}));

import { SettingsPanel } from "@/components/teams/panels/SettingsPanel";

describe("SettingsPanel", () => {
  it("hydrates fields from server data", () => {
    render(<SettingsPanel />);
    expect(screen.getByLabelText("Display name")).toHaveValue("Acme Co");
    expect(screen.getByLabelText("Description")).toHaveValue("Building things");
  });

  it("shows the operator-controlled disclaimer", () => {
    render(<SettingsPanel />);
    expect(
      screen.getByText(/Adapter, plugin, and instance settings/i),
    ).toBeInTheDocument();
  });

  it("save patches /settings with ONLY {displayName, description}", async () => {
    mockPatch.mockResolvedValue({});
    render(<SettingsPanel />);

    fireEvent.change(screen.getByLabelText("Display name"), {
      target: { value: "New name" },
    });
    fireEvent.click(screen.getByText("Save"));

    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledTimes(1);
    });
    expect(mockPatch).toHaveBeenCalledWith("/settings", {
      displayName: "New name",
      description: "Building things",
    });

    const [, body] = mockPatch.mock.calls[0];
    expect(Object.keys(body as Record<string, unknown>).sort()).toEqual([
      "description",
      "displayName",
    ]);
    // Defense-in-depth: confirm no smuggled fields.
    expect(body).not.toHaveProperty("adapterType");
    expect(body).not.toHaveProperty("adapterConfig");
    expect(body).not.toHaveProperty("plugins");
    expect(body).not.toHaveProperty("instance_id");
  });
});
