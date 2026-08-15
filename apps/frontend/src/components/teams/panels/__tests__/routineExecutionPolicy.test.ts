import { describe, expect, it } from "vitest";
import type { IssueExecutionPolicy } from "@/components/teams/shared/types";
import {
  routineReviewerAgentId,
  withRoutineReviewer,
} from "../routineExecutionPolicy";

const firstReviewer = "11111111-1111-4111-8111-111111111111";
const nextReviewer = "22222222-2222-4222-8222-222222222222";

function reviewerPolicy(): IssueExecutionPolicy {
  return {
    mode: "normal",
    stages: [
      {
        id: "33333333-3333-4333-8333-333333333333",
        type: "review",
        approvalsNeeded: 1,
        participants: [
          {
            id: "44444444-4444-4444-8444-444444444444",
            type: "agent",
            agentId: firstReviewer,
            userId: null,
          },
        ],
      },
    ],
  };
}

describe("routineExecutionPolicy", () => {
  it("builds the native review stage used by generated routine issues", () => {
    const policy = withRoutineReviewer(null, firstReviewer);

    expect(routineReviewerAgentId(policy)).toBe(firstReviewer);
    expect(policy?.stages).toEqual([
      expect.objectContaining({
        type: "review",
        approvalsNeeded: 1,
        participants: [
          expect.objectContaining({
            type: "agent",
            agentId: firstReviewer,
            userId: null,
          }),
        ],
      }),
    ]);
  });

  it("returns explicit null when the only reviewer is removed", () => {
    expect(withRoutineReviewer(reviewerPolicy(), null)).toBeNull();
  });

  it("preserves unrelated policy fields and approval-stage identities on a reviewer swap", () => {
    const policy = {
      ...reviewerPolicy(),
      commentRequired: false,
      maxReviewRounds: 4,
      stages: [
        ...reviewerPolicy().stages!,
        {
          id: "55555555-5555-4555-8555-555555555555",
          type: "approval" as const,
          approvalsNeeded: 1 as const,
          participants: [
            {
              id: "66666666-6666-4666-8666-666666666666",
              type: "agent" as const,
              agentId: "77777777-7777-4777-8777-777777777777",
              userId: null,
            },
          ],
        },
      ],
    } as IssueExecutionPolicy;

    const next = withRoutineReviewer(policy, nextReviewer);

    expect((next as unknown as Record<string, unknown>).commentRequired).toBe(false);
    expect((next as unknown as Record<string, unknown>).maxReviewRounds).toBe(4);
    expect(next?.stages?.find((stage) => stage.type === "review")?.id).toBe(
      "33333333-3333-4333-8333-333333333333",
    );
    expect(
      next?.stages?.find((stage) => stage.type === "review")?.participants[0],
    ).toMatchObject({ type: "agent", agentId: nextReviewer });
    expect(next?.stages?.find((stage) => stage.type === "approval")).toEqual(
      policy.stages?.[1],
    );
  });

  it("keeps an unrelated policy object when its reviewer is removed", () => {
    const policy = {
      ...reviewerPolicy(),
      authorizationPolicy: { mode: "restricted" },
    } as unknown as IssueExecutionPolicy;

    const next = withRoutineReviewer(policy, null);

    expect(next).not.toBeNull();
    expect((next as unknown as Record<string, unknown>).authorizationPolicy).toEqual({
      mode: "restricted",
    });
    expect(next?.stages).toEqual([]);
  });

  it("retains unmodeled human and additional-agent reviewers", () => {
    const additionalAgent = "88888888-8888-4888-8888-888888888888";
    const humanReviewer = "99999999-9999-4999-8999-999999999999";
    const policy: IssueExecutionPolicy = {
      ...reviewerPolicy(),
      stages: [
        {
          ...reviewerPolicy().stages![0],
          participants: [
            reviewerPolicy().stages![0].participants[0],
            { type: "user", userId: humanReviewer, agentId: null },
            { type: "agent", agentId: additionalAgent, userId: null },
          ],
        },
      ],
    };

    const next = withRoutineReviewer(policy, nextReviewer);

    expect(next?.stages?.[0]?.participants).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ type: "agent", agentId: nextReviewer }),
        expect.objectContaining({ type: "user", userId: humanReviewer }),
        expect.objectContaining({ type: "agent", agentId: additionalAgent }),
      ]),
    );
  });
});
