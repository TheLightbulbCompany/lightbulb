import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { ModelEntry } from "@/lib/models";
import type { UseAgentModelsResult } from "@/hooks/useAgentModels";
import type { SessionModelControls } from "@/hooks/useSessionModelControls";

const models: ModelEntry[] = [
  { id: "gpt-5", provider: "openai", name: "GPT-5", available: true, reasoning: true },
  { id: "gpt-4o", provider: "openai", name: "GPT-4o", available: true, reasoning: false },
];

const agentModels = vi.fn<(agentId: string | null) => UseAgentModelsResult>(() => ({
  models,
  defaultModel: "openai/gpt-5",
  isLoading: false,
  usingFallback: false,
}));
vi.mock("@/hooks/useAgentModels", () => ({ useAgentModels: (a: string | null) => agentModels(a) }));

const setModel = vi.fn<(e: ModelEntry) => Promise<void>>(async () => {});
const setThinkingLevel = vi.fn<(l: string) => Promise<void>>(async () => {});
const setFastMode = vi.fn<(f: boolean) => Promise<void>>(async () => {});
vi.mock("@/hooks/useSessionModelControls", () => ({
  useSessionModelControls: (): SessionModelControls => ({ setModel, setThinkingLevel, setFastMode }),
  LiveSessionModelSwitchError: class extends Error {},
}));

import { ModelEffortPill } from "@/components/chat/ModelEffortPill";

const base = {
  agentId: "agent_a",
  activeSessionId: null,
  isStreaming: false,
  selectedModelRef: "openai/gpt-5",
  thinkingLevel: "medium",
  fastMode: false,
  providerChoice: "bedrock_claude" as string | null,
  byoProvider: null as string | null,
};

beforeEach(() => {
  setModel.mockClear();
  setThinkingLevel.mockClear();
  setFastMode.mockClear();
});

describe("ModelEffortPill", () => {
  it("shows the Reasoning section for a reasoning model", () => {
    render(<ModelEffortPill {...base} />);
    fireEvent.click(screen.getByTestId("model-effort-pill"));
    expect(screen.getByText("Reasoning")).toBeInTheDocument();
  });

  it("hides the Reasoning section when the selected model is non-reasoning", () => {
    render(<ModelEffortPill {...base} selectedModelRef="openai/gpt-4o" />);
    fireEvent.click(screen.getByTestId("model-effort-pill"));
    expect(screen.queryByText("Reasoning")).not.toBeInTheDocument();
  });

  it("shows Speed (fast-mode) only for BYO-OpenAI", () => {
    const { rerender } = render(<ModelEffortPill {...base} />);
    fireEvent.click(screen.getByTestId("model-effort-pill"));
    expect(screen.queryByText("Speed")).not.toBeInTheDocument();

    rerender(<ModelEffortPill {...base} providerChoice="byo_key" byoProvider="openai" />);
    expect(screen.getByText("Speed")).toBeInTheDocument();
  });

  it("calls setModel with the exact catalog entry (provider+id) on select", () => {
    render(<ModelEffortPill {...base} selectedModelRef="openai/gpt-4o" />);
    fireEvent.click(screen.getByTestId("model-effort-pill"));
    fireEvent.click(screen.getByRole("button", { name: /GPT-5/ }));
    expect(setModel).toHaveBeenCalledWith(models[0]);
  });

  it("disables model rows while a run is active", () => {
    render(<ModelEffortPill {...base} selectedModelRef="openai/gpt-4o" isStreaming />);
    fireEvent.click(screen.getByTestId("model-effort-pill"));
    expect(screen.getByRole("button", { name: /GPT-5/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /GPT-5/ }));
    expect(setModel).not.toHaveBeenCalled();
  });
});
