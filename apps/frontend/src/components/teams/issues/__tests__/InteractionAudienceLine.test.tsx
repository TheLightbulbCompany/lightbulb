import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { IssueThreadInteractionCard } from "@/components/teams/issues/IssueThreadInteractionCard";
import type { CompanyAgent, RequestConfirmationInteraction } from "@/components/teams/shared/types";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: undefined, isLoading: false, error: null, mutate: vi.fn() }),
    post: vi.fn(),
    mutate: vi.fn(),
  }),
}));

const agentMap = new Map<string, CompanyAgent>([
  ["agt_ada", { id: "agt_ada", name: "Ada" } as CompanyAgent],
  ["agt_friday", { id: "agt_friday", name: "Friday" } as CompanyAgent],
]);

function confirmation(over: Partial<RequestConfirmationInteraction> = {}): RequestConfirmationInteraction {
  return {
    id: "int_c",
    companyId: "co_1",
    issueId: "iss_1",
    kind: "request_confirmation",
    status: "pending",
    continuationPolicy: "none",
    createdAt: "2026-10-18T10:00:00Z",
    updatedAt: "2026-10-18T10:00:00Z",
    createdByAgentId: "agt_friday",
    payload: { version: 1, prompt: "Ship it?" },
    requestedResolverPolicy: "anyone",
    effectiveResolverPolicy: "anyone",
    resolverPolicyProvenance: "inherited",
    effectiveResolverPolicySource: "requested",
    ...over,
  };
}

const renderCard = (interaction: RequestConfirmationInteraction) =>
  render(<IssueThreadInteractionCard interaction={interaction} agentMap={agentMap} />);

describe("interaction audience line", () => {
  it("names the agent addressee as the one who can answer", () => {
    renderCard(confirmation({ addresseeAgentId: "agt_ada" }));
    const line = screen.getByTestId("interaction-audience");
    expect(line).toHaveTextContent("Addressed — Only Ada or a person on the board can respond.");
  });

  it("uses the board audience wording for human_only", () => {
    renderCard(confirmation({ requestedResolverPolicy: "human_only", effectiveResolverPolicy: "human_only", resolverPolicyProvenance: "explicit" }));
    expect(screen.getByTestId("interaction-audience")).toHaveTextContent(
      "Human only — Only a person on the board can respond — agents cannot resolve this card.",
    );
  });

  it("explains a narrowing the requester did not ask for", () => {
    renderCard(confirmation({ effectiveResolverPolicy: "human_only", effectiveResolverPolicySource: "governed_action" }));
    expect(screen.getByTestId("interaction-audience-note")).toHaveTextContent(
      "This card runs a governed action, so it stays human-only whatever audience was requested.",
    );
  });

  it("renders no line when the row carries no policy", () => {
    renderCard(confirmation({ effectiveResolverPolicy: undefined, requestedResolverPolicy: undefined }));
    expect(screen.queryByTestId("interaction-audience")).toBeNull();
  });

  it("renders no line once the interaction is resolved", () => {
    renderCard(confirmation({ status: "accepted" }));
    expect(screen.queryByTestId("interaction-audience")).toBeNull();
  });
});
