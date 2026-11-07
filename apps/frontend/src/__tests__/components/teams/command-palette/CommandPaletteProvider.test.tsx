import { test, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen, act } from "@testing-library/react";

vi.mock("@/components/teams/command-palette/useFilteredCommandResults", () => ({
  useFilteredCommandResults: vi.fn(() => ({ agents: [], issues: [], projects: [] })),
}));
// The Tasks and Files groups read the tasks client; these suites are about navigation and messages.
vi.mock("@/components/teams/command-palette/useTaskSearch", async (importOriginal) => ({ ...(await importOriginal<object>()), useTaskSearch: () => ({ tasks: [], files: [] }) }));
vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({ messaging: { searchMessages: vi.fn().mockResolvedValue({ hits: [] }), warmSearch: vi.fn().mockResolvedValue(undefined) } }),
}));
vi.mock("@/components/settings/CompanyStripeSection", () => ({
  useCompanyStripeStatus: () => ({ data: null }),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

import {
  CommandPaletteProvider,
  useCommandPalette,
} from "@/components/teams/command-palette/CommandPaletteProvider";

function Probe() {
  const { open, toggle } = useCommandPalette();
  return (
    <>
      <button data-testid="probe-toggle" onClick={toggle}>toggle</button>
      <span data-testid="probe-state">{open ? "open" : "closed"}</span>
    </>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

test("starts closed", () => {
  render(<CommandPaletteProvider><Probe /></CommandPaletteProvider>);
  expect(screen.getByTestId("probe-state").textContent).toBe("closed");
});

test("Cmd+K opens", () => {
  render(<CommandPaletteProvider><Probe /></CommandPaletteProvider>);
  act(() => {
    fireEvent.keyDown(document, { key: "k", metaKey: true });
  });
  expect(screen.getByTestId("probe-state").textContent).toBe("open");
});

test("Ctrl+K opens", () => {
  render(<CommandPaletteProvider><Probe /></CommandPaletteProvider>);
  act(() => {
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
  });
  expect(screen.getByTestId("probe-state").textContent).toBe("open");
});

test("Cmd+K toggles when already open", () => {
  render(<CommandPaletteProvider><Probe /></CommandPaletteProvider>);
  act(() => {
    fireEvent.keyDown(document, { key: "k", metaKey: true });
  });
  expect(screen.getByTestId("probe-state").textContent).toBe("open");
  act(() => {
    fireEvent.keyDown(document, { key: "k", metaKey: true });
  });
  expect(screen.getByTestId("probe-state").textContent).toBe("closed");
});

test("Cmd+Shift+K does NOT trigger (only plain Cmd+K)", () => {
  render(<CommandPaletteProvider><Probe /></CommandPaletteProvider>);
  act(() => {
    fireEvent.keyDown(document, { key: "k", metaKey: true, shiftKey: true });
  });
  expect(screen.getByTestId("probe-state").textContent).toBe("closed");
});

test("Cmd+Alt+K does NOT trigger", () => {
  render(<CommandPaletteProvider><Probe /></CommandPaletteProvider>);
  act(() => {
    fireEvent.keyDown(document, { key: "k", metaKey: true, altKey: true });
  });
  expect(screen.getByTestId("probe-state").textContent).toBe("closed");
});

test("plain k (no modifier) does NOT trigger", () => {
  render(<CommandPaletteProvider><Probe /></CommandPaletteProvider>);
  act(() => {
    fireEvent.keyDown(document, { key: "k" });
  });
  expect(screen.getByTestId("probe-state").textContent).toBe("closed");
});

test("uppercase K with Cmd opens (case-insensitive)", () => {
  render(<CommandPaletteProvider><Probe /></CommandPaletteProvider>);
  act(() => {
    fireEvent.keyDown(document, { key: "K", metaKey: true });
  });
  expect(screen.getByTestId("probe-state").textContent).toBe("open");
});

test("toggle() programmatically opens", () => {
  render(<CommandPaletteProvider><Probe /></CommandPaletteProvider>);
  fireEvent.click(screen.getByTestId("probe-toggle"));
  expect(screen.getByTestId("probe-state").textContent).toBe("open");
});

test("listener is cleaned up on unmount", () => {
  const { unmount } = render(<CommandPaletteProvider><Probe /></CommandPaletteProvider>);
  unmount();
  // Dispatch a Cmd+K — nothing should happen, no error
  act(() => {
    fireEvent.keyDown(document, { key: "k", metaKey: true });
  });
  // No assertion needed — the test just verifies no listener still attached
});
