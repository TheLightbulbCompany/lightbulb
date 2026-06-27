import { describe, it, expect } from "vitest";
import { buildTeamsThreadItems } from "../teams-thread-items";
import type {
  IssueComment,
  RequestConfirmationInteraction,
  SuggestTasksInteraction,
} from "@/components/teams/shared/types";

function comment(id: string, createdAt: string, body = "hi"): IssueComment {
  return { id, body, createdAt };
}

function suggestTasks(
  id: string,
  createdAt: string,
): SuggestTasksInteraction {
  return {
    id,
    companyId: "co_1",
    issueId: "iss_1",
    kind: "suggest_tasks",
    status: "pending",
    continuationPolicy: "none",
    createdAt,
    updatedAt: createdAt,
    payload: { version: 1, tasks: [] },
  };
}

function confirmation(
  id: string,
  createdAt: string,
): RequestConfirmationInteraction {
  return {
    id,
    companyId: "co_1",
    issueId: "iss_1",
    kind: "request_confirmation",
    status: "pending",
    continuationPolicy: "none",
    createdAt,
    updatedAt: createdAt,
    payload: { version: 1, prompt: "ok?" },
  };
}

describe("buildTeamsThreadItems", () => {
  it("interleaves comments and interactions in ascending createdAt order", () => {
    const comments: IssueComment[] = [
      comment("c1", "2026-06-20T10:00:00.000Z"),
      comment("c2", "2026-06-20T12:00:00.000Z"),
    ];
    const interactions = [
      suggestTasks("i1", "2026-06-20T11:00:00.000Z"),
      confirmation("i2", "2026-06-20T13:00:00.000Z"),
    ];

    const items = buildTeamsThreadItems({ comments, interactions });

    expect(items.map((item) => [item.kind, item.id])).toEqual([
      ["comment", "c1"],
      ["interaction", "i1"],
      ["comment", "c2"],
      ["interaction", "i2"],
    ]);
  });

  it("carries the original comment/interaction payload on each item", () => {
    const items = buildTeamsThreadItems({
      comments: [comment("c1", "2026-06-20T10:00:00.000Z", "body-text")],
      interactions: [suggestTasks("i1", "2026-06-20T11:00:00.000Z")],
    });

    const commentItem = items[0];
    const interactionItem = items[1];
    expect(commentItem.kind).toBe("comment");
    if (commentItem.kind === "comment") {
      expect(commentItem.comment.body).toBe("body-text");
    }
    expect(interactionItem.kind).toBe("interaction");
    if (interactionItem.kind === "interaction") {
      expect(interactionItem.interaction.kind).toBe("suggest_tasks");
    }
  });

  it("breaks createdAt ties deterministically by id", () => {
    const ts = "2026-06-20T10:00:00.000Z";
    const items = buildTeamsThreadItems({
      comments: [comment("b", ts), comment("a", ts)],
      interactions: [confirmation("c", ts)],
    });
    expect(items.map((item) => item.id)).toEqual(["a", "b", "c"]);
  });

  it("handles empty inputs and a Date-typed createdAt", () => {
    expect(buildTeamsThreadItems({ comments: [], interactions: [] })).toEqual([]);

    const dated = suggestTasks("i1", "2026-06-20T10:00:00.000Z");
    dated.createdAt = new Date("2026-06-20T09:00:00.000Z");
    const items = buildTeamsThreadItems({
      comments: [comment("c1", "2026-06-20T10:00:00.000Z")],
      interactions: [dated],
    });
    // The Date-typed interaction (09:00) sorts before the 10:00 comment.
    expect(items.map((item) => item.id)).toEqual(["i1", "c1"]);
  });
});
