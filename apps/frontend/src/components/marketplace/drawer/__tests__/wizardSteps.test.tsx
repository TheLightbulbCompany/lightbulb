import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { BundleStep, PricingStep, ReviewStep } from "../wizardSteps";
import type { PublishForm } from "../PublishWizard";
import type { ArtifactPreview } from "@/lib/marketplace/api";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
}));

// PricingStep calls useApi().get("/marketplace/payouts/dashboard") to gate
// the free→paid switch. The mock factory returns a fresh vi.fn() each render
// so individual tests can swap responses via mockResolvedValueOnce.
const mockApiGet = vi.fn();
const mockApiPost = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({
    get: mockApiGet,
    post: mockApiPost,
    put: vi.fn(),
    del: vi.fn(),
  }),
}));

vi.mock("@/lib/marketplace/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/marketplace/api")>(
    "@/lib/marketplace/api",
  );
  return {
    ...actual,
    artifactPreview: vi.fn(),
  };
});

import { artifactPreview } from "@/lib/marketplace/api";

const mockArtifactPreview = artifactPreview as ReturnType<typeof vi.fn>;

function baseForm(overrides: Partial<PublishForm> = {}): PublishForm {
  return {
    agentId: "agent_xyz",
    name: "Atlas",
    tagline: "Clears the inbox",
    description: "",
    category: "Operations",
    tags: [],
    priceTier: "free",
    priceDollars: 10,
    bundleMcps: [],
    bundleSkills: [],
    ...overrides,
  };
}

function basePreview(overrides: Partial<ArtifactPreview> = {}): ArtifactPreview {
  return {
    agent_id: "agent_xyz",
    available_mcps: [],
    available_skills: [],
    persona_files: [],
    safety_flags: [],
    needs_config_patch: false,
    ...overrides,
  };
}

describe("BundleStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders one checkbox per available MCP, all initially unchecked", async () => {
    mockArtifactPreview.mockResolvedValue(
      basePreview({ available_mcps: ["jira-mcp", "slack-mcp"] }),
    );
    const onChange = vi.fn();
    render(<BundleStep form={baseForm()} onChange={onChange} />);

    const jiraBox = await screen.findByLabelText("bundle MCP jira-mcp");
    const slackBox = await screen.findByLabelText("bundle MCP slack-mcp");
    expect(jiraBox).not.toBeChecked();
    expect(slackBox).not.toBeChecked();
  });

  it("toggling an MCP checkbox calls onChange with the new selection", async () => {
    mockArtifactPreview.mockResolvedValue(
      basePreview({ available_mcps: ["jira-mcp"] }),
    );
    const onChange = vi.fn();
    render(<BundleStep form={baseForm()} onChange={onChange} />);

    const cb = await screen.findByLabelText("bundle MCP jira-mcp");
    fireEvent.click(cb);
    expect(onChange).toHaveBeenCalledWith({ bundleMcps: ["jira-mcp"] });
  });

  it("renders skill bucket tags so the seller knows what 'configured' means", async () => {
    mockArtifactPreview.mockResolvedValue(
      basePreview({
        available_skills: [
          { name: "search", bucket: "bundled" },
          { name: "jira", bucket: "entries" },
          { name: "custom-skill", bucket: "workspace" },
        ],
      }),
    );
    render(<BundleStep form={baseForm()} onChange={vi.fn()} />);

    await screen.findByLabelText("bundle skill search");
    expect(screen.getByText("built-in")).toBeInTheDocument();
    expect(screen.getByText("configured")).toBeInTheDocument();
    expect(screen.getByText("in workspace")).toBeInTheDocument();
  });

  it("toggling a skill checkbox calls onChange with the new selection", async () => {
    mockArtifactPreview.mockResolvedValue(
      basePreview({
        available_skills: [{ name: "jira", bucket: "entries" }],
      }),
    );
    const onChange = vi.fn();
    render(<BundleStep form={baseForm()} onChange={onChange} />);
    const cb = await screen.findByLabelText("bundle skill jira");
    fireEvent.click(cb);
    expect(onChange).toHaveBeenCalledWith({ bundleSkills: ["jira"] });
  });

  it("lists persona files in the always-included section", async () => {
    mockArtifactPreview.mockResolvedValue(
      basePreview({ persona_files: ["IDENTITY.md", "SOUL.md", "USER.md"] }),
    );
    render(<BundleStep form={baseForm()} onChange={vi.fn()} />);
    await screen.findByText(/Personality files/);
    expect(screen.getByText("IDENTITY.md")).toBeInTheDocument();
    expect(screen.getByText("SOUL.md")).toBeInTheDocument();
    expect(screen.getByText("USER.md")).toBeInTheDocument();
  });

  it("renders nothing-to-bundle copy when the agent has no MCPs / skills", async () => {
    mockArtifactPreview.mockResolvedValue(basePreview());
    render(<BundleStep form={baseForm()} onChange={vi.fn()} />);
    await screen.findByText(/No MCP servers configured/);
    expect(screen.getByText(/no skills configured/)).toBeInTheDocument();
  });

  it("surfaces preview errors", async () => {
    mockArtifactPreview.mockRejectedValue(new Error("network down"));
    render(<BundleStep form={baseForm()} onChange={vi.fn()} />);
    await screen.findByRole("alert");
    expect(screen.getByRole("alert")).toHaveTextContent("network down");
  });
});

describe("ReviewStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders bundled-summary counts from form selection + preview persona_files", async () => {
    mockArtifactPreview.mockResolvedValue(
      basePreview({
        persona_files: ["IDENTITY.md", "SOUL.md"],
        available_skills: [{ name: "jira", bucket: "entries" }],
      }),
    );
    render(
      <ReviewStep
        form={baseForm({
          bundleMcps: ["jira-mcp", "slack-mcp"],
          bundleSkills: ["jira"],
        })}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("review-bundle-summary")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("review-bundle-summary")).toHaveTextContent(
      "Bundled: 2 MCPs · 1 skill · 2 persona files",
    );
  });

  it("shows the config-patch warning when an MCP is selected", async () => {
    mockArtifactPreview.mockResolvedValue(basePreview());
    render(
      <ReviewStep form={baseForm({ bundleMcps: ["jira-mcp"] })} />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("review-config-patch-warning")).toBeInTheDocument(),
    );
  });

  it("hides the config-patch warning for a workspace-only listing", async () => {
    mockArtifactPreview.mockResolvedValue(
      basePreview({
        available_skills: [{ name: "custom-skill", bucket: "workspace" }],
      }),
    );
    render(
      <ReviewStep
        form={baseForm({ bundleMcps: [], bundleSkills: ["custom-skill"] })}
      />,
    );
    await screen.findByTestId("review-bundle-summary");
    expect(screen.queryByTestId("review-config-patch-warning")).not.toBeInTheDocument();
  });

  it("renders safety_flags inline with file + line", async () => {
    mockArtifactPreview.mockResolvedValue(
      basePreview({
        safety_flags: [
          {
            pattern: "email-address",
            severity: "medium",
            file: "USER.md",
            line: 3,
            snippet: "Email: alice@example.com",
          },
          {
            pattern: "aws-secret-key",
            severity: "high",
            file: "MEMORY.md",
            line: 5,
            snippet: "Old key was AKIAIOSFODNN7EXAMPLE",
          },
        ],
      }),
    );
    render(<ReviewStep form={baseForm()} />);
    await screen.findByTestId("review-safety-flags");
    expect(screen.getByText("email-address")).toBeInTheDocument();
    expect(screen.getByText(/USER\.md:3/)).toBeInTheDocument();
    expect(screen.getByText("aws-secret-key")).toBeInTheDocument();
    expect(screen.getByText(/MEMORY\.md:5/)).toBeInTheDocument();
  });

  it("renders no safety-flags section when scan is clean", async () => {
    mockArtifactPreview.mockResolvedValue(basePreview());
    render(<ReviewStep form={baseForm()} />);
    await screen.findByTestId("review-bundle-summary");
    expect(screen.queryByTestId("review-safety-flags")).not.toBeInTheDocument();
  });
});

describe("PricingStep payout gate", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPost.mockReset();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the payout setup gate when paid is selected but no Connect account exists", async () => {
    mockApiGet.mockResolvedValue({ dashboard_url: null });
    render(<PricingStep form={baseForm({ priceTier: "paid" })} onChange={vi.fn()} />);
    const gate = await screen.findByTestId("payout-setup-gate");
    expect(gate).toBeInTheDocument();
    expect(gate).toHaveTextContent(/Set up payouts first/);
  });

  it("hides the gate when the seller is already onboarded", async () => {
    mockApiGet.mockResolvedValue({ dashboard_url: "https://stripe.com/login/abc" });
    render(<PricingStep form={baseForm({ priceTier: "paid" })} onChange={vi.fn()} />);
    await waitFor(() => expect(mockApiGet).toHaveBeenCalled());
    expect(screen.queryByTestId("payout-setup-gate")).not.toBeInTheDocument();
  });

  it("hides the gate while priceTier is free", async () => {
    // No dashboard fetch is necessary — the gate is bound to paid mode.
    mockApiGet.mockResolvedValue({ dashboard_url: null });
    render(<PricingStep form={baseForm({ priceTier: "free" })} onChange={vi.fn()} />);
    // The hook still fires once on mount, but the gate UI doesn't render.
    await waitFor(() => expect(mockApiGet).toHaveBeenCalled());
    expect(screen.queryByTestId("payout-setup-gate")).not.toBeInTheDocument();
  });

  it("the gate's CTA kicks off the Stripe onboarding flow", async () => {
    mockApiGet.mockResolvedValue({ dashboard_url: null });
    mockApiPost.mockResolvedValue({ onboarding_url: "https://connect.stripe.com/setup/abc" });
    // jsdom's window.location is readonly — replace with a writable mock so
    // we can assert the redirect target without the browser navigating.
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { ...originalLocation, href: "" },
    });
    try {
      render(<PricingStep form={baseForm({ priceTier: "paid" })} onChange={vi.fn()} />);
      const cta = await screen.findByText("Set up payouts →");
      fireEvent.click(cta);
      await waitFor(() => expect(mockApiPost).toHaveBeenCalledWith("/marketplace/payouts/onboard", {}));
      expect(window.location.href).toBe("https://connect.stripe.com/setup/abc");
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        writable: true,
        value: originalLocation,
      });
    }
  });
});
