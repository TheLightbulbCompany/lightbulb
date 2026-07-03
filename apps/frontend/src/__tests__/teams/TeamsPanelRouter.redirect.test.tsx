// apps/frontend/src/__tests__/teams/TeamsPanelRouter.redirect.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";

import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";

beforeEach(() => {
  window.history.replaceState(null, "", "/chat?panel=teams-approvals");
});

describe("TeamsPanelRouter retirement", () => {
  it("remaps the retired approvals panel key to the inbox in place", () => {
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");
    const { container } = render(<TeamsPanelRouter panel="approvals" />);
    // Renders nothing itself — the shell re-renders with the aliased key.
    expect(container).toBeEmptyDOMElement();
    const target = replaceStateSpy.mock.calls.at(-1)?.[2] as string;
    expect(target).toContain("panel=teams-inbox");
    replaceStateSpy.mockRestore();
  });
});
