import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/error";
import { answerHold, holdAnswers, holdFocusId, holdText, isHoldFeedItem, isHoldFocus, type NetworkHold } from "./holds";

const message: NetworkHold = {
  hold_id: "h1", holding_member: "m1", holding_computer: "Studio", target_session: "studio/claude",
  from: "aden/claude", label: "Aden", kind: "message", created_at: "2026-10-23T10:00:00Z", agent: "Claude",
};
const permission: NetworkHold = { ...message, hold_id: "h2", kind: "permission", title: "Bash" };
const labels = (hold: NetworkHold) => holdAnswers(hold).map((answer) => answer.label);

describe("network holds", () => {
  it("cardShowsNoMessageText: the line names sender, session and computer only", () => {
    expect(holdText({ ...message, preview: "the secret plan" } as NetworkHold)).toBe("Aden wants to message your Claude session on Studio.");
    expect(holdText({ ...message, agent: undefined })).toBe("Aden wants to message your studio/claude session on Studio.");
  });

  it("threeAnswersForAMessageHold", () => {
    expect(holdAnswers(message).map(({ label, verdict, scope }) => [label, verdict, scope])).toEqual([
      ["Allow once", "accept", "once"], ["Allow Aden always", "accept", "person"], ["Decline", "refuse", "once"],
    ]);
  });

  it("alwaysAllowOnlyForOwnWorkspaceAgents", () => {
    expect(labels({ ...message, own_cloud: true })).toEqual(["Allow once", "Allow Aden always", "Always allow", "Decline"]);
    expect(holdAnswers({ ...message, own_cloud: true })[2].scope).toBe("own_cloud");
    expect(labels({ ...message, own_cloud: false })).not.toContain("Always allow");
  });

  it("permissionHoldOffersOnceAndDecline", () => {
    expect(labels({ ...permission, own_cloud: true })).toEqual(["Allow once", "Decline"]);
    expect(holdText(permission)).toBe("Claude on Studio asks permission: Bash.");
    expect(holdText({ ...permission, title: "x".repeat(80) })).toBe(`Claude on Studio asks permission: ${"x".repeat(60)}.`);
    expect(holdText({ ...permission, title: undefined })).toBe("Claude on Studio asks permission.");
  });

  it("posts the decision and reads the result line", async () => {
    const [once, person, decline] = holdAnswers(message);
    const post = vi.fn().mockResolvedValueOnce({ result: "applied" });
    expect(await answerHold({ post }, message, person)).toBe("Allowed.");
    expect(post).toHaveBeenCalledWith("/network/holds/h1/decision", { holding_member: "m1", verdict: "accept", scope: "person" });
    post.mockResolvedValueOnce({ result: "applied" });
    expect(await answerHold({ post }, message, decline)).toBe("Declined.");
    post.mockResolvedValueOnce({ result: "not_open" });
    expect(await answerHold({ post }, message, once)).toBe("Answered on your Mac.");
    post.mockRejectedValueOnce(new ApiError(404, { detail: "not_found" }));
    expect(await answerHold({ post }, message, once)).toBe("Answered on your Mac.");
    post.mockRejectedValueOnce(new ApiError(500, null));
    await expect(answerHold({ post }, message, once)).rejects.toBeInstanceOf(ApiError);
  });

  it("names a hold's focus and its feed item one way", () => {
    expect(holdFocusId("h1")).toBe("network_hold:h1");
    expect(isHoldFocus("network_hold:h1")).toBe(true);
    expect(isHoldFocus("login:x")).toBe(false);
    expect(isHoldFocus(undefined)).toBe(false);
    expect(isHoldFeedItem({ kind: "network_hold" })).toBe(true);
    expect(isHoldFeedItem({ kind: "mention" })).toBe(false);
  });
});
