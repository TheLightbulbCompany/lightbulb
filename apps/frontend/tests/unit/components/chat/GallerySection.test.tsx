import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { GallerySection } from "@/components/chat/GallerySection";

const { deployMock, refreshMock } = vi.hoisted(() => ({
  deployMock: vi.fn().mockResolvedValue({
    agent_id: "agent_new",
    name: "Pitch",
    slug: "pitch",
    version: 3,
    skills_added: [],
    plugins_enabled: [],
  }),
  refreshMock: vi.fn(),
}));

vi.mock("@/hooks/useCatalog", () => ({
  useCatalog: () => ({
    agents: [
      { slug: "pitch", name: "Pitch", version: 3, emoji: "🎯", vibe: "", description: "",
        suggested_model: "", suggested_channels: [], required_skills: [], required_plugins: [] },
    ],
    isLoading: false,
    deploy: deployMock,
    refresh: refreshMock,
  }),
}));

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ refresh: refreshMock }),
}));

describe("GallerySection", () => {
  // The global test setup calls vi.restoreAllMocks() in afterEach, which
  // strips mockResolvedValue. Re-apply it before each test.
  beforeEach(() => {
    deployMock.mockResolvedValue({
      agent_id: "agent_new",
      name: "Pitch",
      slug: "pitch",
      version: 3,
      skills_added: [],
      plugins_enabled: [],
    });
  });

  it("renders the header and each agent row", () => {
    render(<GallerySection onAgentDeployed={vi.fn()} />);
    expect(
      screen.getByRole("heading", { name: /from the marketplace/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("Pitch")).toBeInTheDocument();
  });

  it("calls onAgentDeployed with new agent info after deploy", async () => {
    const onAgentDeployed = vi.fn();
    render(<GallerySection onAgentDeployed={onAgentDeployed} />);
    await userEvent.click(screen.getByRole("button", { name: /deploy pitch/i }));
    await vi.waitFor(() => expect(onAgentDeployed).toHaveBeenCalled());
    expect(onAgentDeployed.mock.calls[0][0]).toMatchObject({ agent_id: "agent_new" });
  });
});
