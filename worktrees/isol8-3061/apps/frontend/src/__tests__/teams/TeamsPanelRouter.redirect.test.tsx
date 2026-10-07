// apps/frontend/src/__tests__/teams/TeamsPanelRouter.redirect.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";

import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";

describe("TeamsPanelRouter alias handling", () => {
  it("no longer rewrites the URL for a retired key — the shell resolves aliases upstream", () => {
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");
    // The router used to remap approvals→inbox itself via replaceState. That
    // now lives in resolvePanelAlias (covered by panelRegistry.test.ts) and is
    // applied by the shell before the router ever sees a retired key, so the
    // router must not touch the URL.
    render(<TeamsPanelRouter panel="approvals" />);
    expect(replaceStateSpy).not.toHaveBeenCalled();
    replaceStateSpy.mockRestore();
  });
});
