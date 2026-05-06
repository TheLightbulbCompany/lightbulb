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

  test("falls back to actorId when actor identity is otherwise missing", () => {
    render(
      <ActivityRow
        event={makeEvent({ actorType: null, actorId: "fallback-actor" })}
      />
    );
    expect(screen.getByText("fallback-actor")).toBeInTheDocument();
  });

  test("wraps in a link to the entity when entityType+entityId are present", () => {
    render(
      <ActivityRow
        event={makeEvent({ entityType: "issue", entityId: "iss_xyz" })}
      />
    );
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/teams/issues/iss_xyz");
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
