import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

vi.mock("@/components/workspace/workspaceController", () => ({ openChatPanelWithDraft: vi.fn() }));
vi.mock("@/lib/focusComposerRequest", () => ({ requestFocusComposer: vi.fn() }));
vi.mock("@/lib/shellNav", () => ({ navigateShell: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyError: vi.fn() }));
vi.mock("@/hooks/useAgents", () => ({ useAgents: vi.fn() }));

import { useAskCeo } from "../useAskCeo";
import { openChatPanelWithDraft } from "@/components/workspace/workspaceController";
import { requestFocusComposer } from "@/lib/focusComposerRequest";
import { navigateShell } from "@/lib/shellNav";
import { notifyError } from "@/lib/notify";
import { useAgents } from "@/hooks/useAgents";

describe("useAskCeo", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useAgents).mockReturnValue({ defaultId: "friday" } as unknown as ReturnType<typeof useAgents>);
  });

  it("opens the dock chat with the draft when the desktop shell takes it", () => {
    vi.mocked(openChatPanelWithDraft).mockReturnValue(true);
    const { result } = renderHook(() => useAskCeo());
    result.current("Why did revenue drop?");
    expect(openChatPanelWithDraft).toHaveBeenCalledWith("Why did revenue drop?");
    expect(navigateShell).not.toHaveBeenCalled();
  });

  it("falls back when no dock is mounted: records the draft, selects the agent, swaps the URL", () => {
    vi.mocked(openChatPanelWithDraft).mockReturnValue(false);
    const dispatched: string[] = [];
    const spy = vi.spyOn(window, "dispatchEvent").mockImplementation((e) => { dispatched.push(e.type); return true; });
    const { result } = renderHook(() => useAskCeo());
    result.current("Who are our new customers?");
    expect(requestFocusComposer).toHaveBeenCalledWith("friday", "Who are our new customers?");
    expect(dispatched).toEqual(["selectAgent", "getstarted:focus-composer"]);
    expect(navigateShell).toHaveBeenCalledWith("/chat");
    spy.mockRestore();
  });

  it("says the CEO is not ready when there is no default agent and no dock", () => {
    vi.mocked(openChatPanelWithDraft).mockReturnValue(false);
    vi.mocked(useAgents).mockReturnValue({ defaultId: null } as unknown as ReturnType<typeof useAgents>);
    const { result } = renderHook(() => useAskCeo());
    result.current("anything");
    expect(notifyError).toHaveBeenCalledWith("Your CEO isn't ready yet — try again in a moment.");
    expect(navigateShell).not.toHaveBeenCalled();
  });
});
