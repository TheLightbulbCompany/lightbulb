// Tests for ActivityRow: action verb mapping, relative-time rendering,
// actor identity rendering (system / agent / user), and entity-link wrapping.

import { describe, test, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ActivityRow } from "@/components/teams/dashboard/ActivityRow";
import type { ActivityEvent } from "@/components/teams/dashboard/hooks/useActivityFeed";

function makeEvent(over: Partial<ActivityEvent> = {}): ActivityEvent {
  return {
    id: "evt_1",
    action: "issue.created",
    actorType: "user",
    actorId: "u_1",
    entityType: "issue",
    entityId: "iss_1",
    createdAt: new Date().toISOString(),
    ...over,
  };
}

describe("ActivityRow", () => {
  test("renders the verb mapped from the action", () => {
    render(<ActivityRow event={makeEvent({ action: "issue.created" })} />);
    expect(screen.getByText("created")).toBeInTheDocument();
  });

  test("falls back to raw action when verb is unknown", () => {
    render(<ActivityRow event={makeEvent({ action: "weird.unknown" })} />);
    expect(screen.getByText("weird.unknown")).toBeInTheDocument();
  });

  test("renders relative time", () => {
    render(
      <ActivityRow event={makeEvent({ createdAt: new Date().toISOString() })} />
    );
    expect(screen.getByText(/just now|m ago|h ago|d ago/i)).toBeInTheDocument();
  });

  test("renders 'System' as actor when actorType=system", () => {
    render(
      <ActivityRow event={makeEvent({ actorType: "system", actorId: null })} />
    );
    expect(screen.getByText("System")).toBeInTheDocument();
  });

  test("passes a short actorId through unshortened when identity is otherwise missing", () => {
    render(
      <ActivityRow event={makeEvent({ actorType: null, actorId: "u_1234" })} />
    );
    // <= 12 chars passes through unshortened.
    expect(screen.getByText("u_1234")).toBeInTheDocument();
  });

  test("prefers the BFF-enriched actorName over the raw actorId", () => {
    render(
      <ActivityRow
        event={makeEvent({
          actorType: "user",
          actorId: "pcu_raw_principal_id",
          actorName: "Ada Lovelace",
        })}
      />
    );
    expect(screen.getByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.queryByText("pcu_raw_principal_id")).toBeNull();
  });

  test("falls back to actorEmail when actorName is absent", () => {
    render(
      <ActivityRow
        event={makeEvent({
          actorType: "user",
          actorId: "pcu_raw_principal_id",
          actorEmail: "ada@example.com",
        })}
      />
    );
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
  });

  test("shortens a long raw user actorId when no name/email resolves", () => {
    render(
      <ActivityRow
        event={makeEvent({
          actorType: "user",
          actorId: "abcdef0123456789ngid",
        })}
      />
    );
    // first 6 + … + last 4.
    expect(screen.getByText("abcdef…ngid")).toBeInTheDocument();
    expect(screen.queryByText("abcdef0123456789ngid")).toBeNull();
  });

  test("resolves an agent actor id to the roster name", () => {
    render(
      <ActivityRow
        event={makeEvent({ actorType: "agent", actorId: "agent_friday_uuid" })}
        agentNames={new Map([["agent_friday_uuid", "Friday"]])}
      />
    );
    expect(screen.getByText("Friday")).toBeInTheDocument();
  });

  test("resolves an agent entity id to the roster name", () => {
    render(
      <ActivityRow
        event={makeEvent({
          action: "agent.updated",
          actorType: "system",
          actorId: null,
          entityType: "agent",
          entityId: "agent_atlas_uuid",
        })}
        agentNames={new Map([["agent_atlas_uuid", "Atlas"]])}
      />
    );
    expect(screen.getByText("Atlas")).toBeInTheDocument();
    expect(screen.queryByText("agent_atlas_uuid")).toBeNull();
  });

  test("shortens an unresolved long entity id instead of dumping the full UUID", () => {
    render(
      <ActivityRow
        event={makeEvent({
          entityType: "issue",
          entityId: "0123456789abcdef-uuid",
        })}
      />
    );
    expect(screen.getByText("012345…uuid")).toBeInTheDocument();
    expect(screen.queryByText("0123456789abcdef-uuid")).toBeNull();
  });

  test("wraps in a link to the entity when entityType+entityId are present", () => {
    render(
      <ActivityRow
        event={makeEvent({ entityType: "issue", entityId: "iss_xyz" })}
      />
    );
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=iss_xyz",
    );
  });

  test("does not wrap in a link for unsupported entity types", () => {
    render(
      <ActivityRow
        event={makeEvent({ entityType: "mystery", entityId: "x" })}
      />
    );
    expect(screen.queryByRole("link")).toBeNull();
  });
});
