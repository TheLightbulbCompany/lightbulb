import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Capture the props the router hands the Brain panel — this suite is about
// the wiring, not the panel (BrainPanel.test.tsx owns that).
let lastProps: {
  initialPath?: string | null;
  initialTab?: string | null;
  onPathUnavailable?: (path: string) => void;
} = {};
vi.mock("@/components/teams/panels/BrainPanel", () => ({
  BrainPanel: (props: typeof lastProps) => {
    lastProps = props;
    return <div data-testid="brain-panel" />;
  },
}));

import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";

describe("TeamsPanelRouter brain branch", () => {
  // BrainPanel loads via next/dynamic, so it resolves asynchronously.
  it("forwards ?path and clears the durable param when the page is gone", async () => {
    const onUpdateDurableParam = vi.fn();
    render(
      <TeamsPanelRouter
        panel="brain"
        panelParams={{ path: "main/index.md" }}
        onUpdateDurableParam={onUpdateDurableParam}
      />,
    );
    await screen.findByTestId("brain-panel");
    expect(lastProps.initialPath).toBe("main/index.md");

    // A stale `?path=` must strip itself, or a persisted layout re-opens the
    // dead page on every load.
    lastProps.onPathUnavailable?.("main/index.md");
    expect(onUpdateDurableParam).toHaveBeenCalledWith("path", null);
  });
});
