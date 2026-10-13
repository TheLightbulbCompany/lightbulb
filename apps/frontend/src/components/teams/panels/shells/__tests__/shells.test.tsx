// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { SoonPanel } from "../SoonPanel";
import { PublishPanel } from "../../PublishPanel";
import { DomainSitePanel } from "../../DomainSitePanel";
import { Globe } from "lucide-react";

const mockRail = vi.fn();
vi.mock("@/components/settings/ProvisionedResources", () => ({
  useProvisioningRail: () => mockRail(),
}));
const mockConnections = vi.fn();
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => mockConnections() }));

describe("SoonPanel", () => {
  it("states the value and, only when given, the prompt the owner can hand to Friday", () => {
    const { rerender } = render(
      <SoonPanel overline="Company" title="Identity" icon={Globe} value="Make the business a real company." />,
    );
    expect(screen.getByText("Coming soon")).toBeInTheDocument();
    expect(screen.getByText("Make the business a real company.")).toBeInTheDocument();
    // No prompt was given — the agent cannot file paperwork — so none is
    // invented. A prompt answered with "I can't do that yet" is worse than none.
    expect(screen.queryByText(/ask your CEO/i)).not.toBeInTheDocument();

    rerender(
      <SoonPanel
        overline="Growth"
        title="SEO & AEO"
        icon={Globe}
        value="Be the answer."
        prompt="Run a site health check on my website."
      />,
    );
    expect(screen.getByText(/Run a site health check on my website\./)).toBeInTheDocument();
  });
});

describe("PublishPanel", () => {
  it("names only the social providers listed for this owner, and links the real exit", () => {
    // In prod, `instagram` is env-gated and hidden from most owners; X is
    // not. Telling a prod owner to "connect Instagram" is an exit that does
    // not exist for them, so the copy names exactly what /connections lists.
    mockConnections.mockReturnValue({
      connections: [{ provider: "x", status: "disconnected" }],
    });
    render(<PublishPanel />);
    expect(screen.getByText(/No social accounts connected yet/i)).toBeInTheDocument();
    expect(screen.getByText(/Connect X in Integrations/i)).toBeInTheDocument();
    expect(screen.queryByText(/Instagram/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /connect accounts/i })).toHaveAttribute(
      "href",
      "/chat?panel=integrations",
    );
    // A calendar with seven weekday headers, so the owner learns the shape
    // of the tab before there is anything in it.
    for (const d of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) {
      expect(screen.getByText(d)).toBeInTheDocument();
    }
  });

  it("does not tell an owner with Instagram connected that nothing is connected", () => {
    mockConnections.mockReturnValue({
      connections: [
        { provider: "instagram", status: "connected" },
        { provider: "x", status: "disconnected" },
      ],
    });
    render(<PublishPanel />);
    expect(screen.getByText(/Instagram connected\./i)).toBeInTheDocument();
    expect(screen.queryByText(/No social accounts connected/i)).not.toBeInTheDocument();
    // Honest about the missing post store: connected, but nothing to show yet.
    expect(screen.getByText(/Nothing has been scheduled or posted yet/i)).toBeInTheDocument();
  });

  it("says 'not available yet' rather than offering a connect button to nothing", () => {
    mockConnections.mockReturnValue({ connections: [] });
    render(<PublishPanel />);
    expect(screen.getByText(/isn't available on your workspace yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /connect accounts/i })).not.toBeInTheDocument();
  });
});

describe("DomainSitePanel", () => {
  it("shows only domain and hosting resources from the provisioning rail", () => {
    mockRail.mockReturnValue({
      error: null,
      isLoading: false,
      byProvider: new Map([
        // `active` is the only status the rail writes; the name is
        // `<service>-<6 chars>`, never the domain itself.
        ["cloudflare", [{ resource_id: "r1", provider: "cloudflare", service: "domain", status: "active", resource_name: "svc_domain-1a2b3c", env_keys: [], level: "free" }]],
        ["posthog", [{ resource_id: "r2", provider: "posthog", service: "project", status: "active", resource_name: "analytics", env_keys: [], level: "free" }]],
      ]),
    });
    render(<DomainSitePanel />);
    expect(screen.getByText("svc_domain-1a2b3c")).toBeInTheDocument();
    // PostHog is analytics, not a domain or host — it belongs to another tab.
    expect(screen.queryByText("analytics")).not.toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("with nothing provisioned, states the exit as a prompt — provisioning is agent-driven by design", () => {
    mockRail.mockReturnValue({ error: null, isLoading: false, byProvider: new Map() });
    render(<DomainSitePanel />);
    expect(screen.getByText(/No domain or hosting yet/i)).toBeInTheDocument();
    expect(screen.getByText(/Register a domain for the company/)).toBeInTheDocument();
    // No "Buy a domain" button: the owner approves a card Friday files; the
    // rail does the rest. A button here would bypass the approval model.
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("says it could not read, rather than 'nothing provisioned', on a failed read", () => {
    mockRail.mockReturnValue({ error: new Error("502"), isLoading: false, byProvider: new Map() });
    render(<DomainSitePanel />);
    expect(screen.getByText(/couldn't read your provisioned resources/i)).toBeInTheDocument();
    expect(screen.queryByText(/No domain or hosting yet/i)).not.toBeInTheDocument();
  });

  it("does not claim 'nothing provisioned' while the first read is still in flight", () => {
    // Cold SWR cache: empty maps, no error, nothing arrived. On a slow
    // connection an owner WITH a domain saw a false empty state.
    mockRail.mockReturnValue({ error: null, isLoading: true, byProvider: new Map() });
    render(<DomainSitePanel />);
    expect(screen.queryByText(/No domain or hosting yet/i)).not.toBeInTheDocument();
  });
});
