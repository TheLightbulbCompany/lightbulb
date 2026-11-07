// Pins the WorkspaceShell reconciler's URL-logic ordering (pure level — no
// shell render):
//   1. same signature twice → second call is a no-op (lastApplied latch)
//   2. the one-shot strip recomputes the latch so the strip's searchParams
//      churn doesn't re-execute the command
//   3. billing-return params never form a command
//   4. INVARIANT: the panel open/focus path performs ZERO history writes —
//      only pushUrl:true calls may write (spec §6: focus never touches the URL)
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/shellNav", () => ({
  pushSearchParams: vi.fn(),
  replaceSearchParams: vi.fn(),
}));
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";

import { deriveReconcilerAction, isCommandTargetOpen } from "../workspaceShellReconciler";
import { clearPanelFromUrl, commandSignature } from "../urlChannel";
import {
  attachShell,
  getWorkspaceSnapshot,
  openPanel,
  _resetControllerForTests,
} from "../workspaceController";
import { MESSAGES_PANEL_ID, RETIRED_PANEL_ALIASES, describePanel } from "../panelRegistry";

/** Run a URL the way the shell does: reconcile, then open (no pushUrl: the URL carries it). */
function navigate(search: string) {
  const d = deriveReconcilerAction(search, null)!;
  if (d.command?.descriptor.component !== "chat") openPanel(d.panelKey!, d.durableParams, { oneShots: d.command!.oneShots });
  if (d.command?.pane) openPanel(d.command.pane.key, d.command.pane.params);
  return d;
}

beforeEach(() => {
  _resetControllerForTests();
  vi.clearAllMocks();
  attachShell(() => undefined);
});

describe("deriveReconcilerAction — lastApplied latch", () => {
  it("executes a command once, then latches: same signature twice → no-op", () => {
    const first = deriveReconcilerAction("?panel=teams-agents", null);
    expect(first).not.toBeNull();
    expect(first!.command?.descriptor.panelId).toBe("teams-agents");
    expect(first!.panelKey).toBe("teams-agents");
    const second = deriveReconcilerAction("?panel=teams-agents", first!.nextSignature);
    expect(second).toBeNull();
  });

  it("latches even when the URL carries no command (so non-command churn is one-shot)", () => {
    const d = deriveReconcilerAction("?utm_source=x", null);
    expect(d).not.toBeNull();
    expect(d!.command).toBeNull();
    expect(deriveReconcilerAction("?utm_source=x", d!.nextSignature)).toBeNull();
  });

  it("hands openPanel the RAW ?panel value — describePanel owns alias resolution", () => {
    // Pre-resolving here would hide the pre-alias key from describePanel, which
    // branches on it to carry a retired panel's meaning onto its successor.
    expect(deriveReconcilerAction("?panel=teams-org-chart", null)!.panelKey).toBe(
      "teams-org-chart",
    );
  });

  it("separates raw durable params (URL grammar) from one-shots", () => {
    const d = deriveReconcilerAction(
      "?panel=agent-inspector&agent=a-1&inspectorTab=channels&action=go",
      null,
    );
    expect(d!.durableParams).toEqual({ agent: "a-1", inspectorTab: "channels" });
    expect(d!.command?.oneShots).toEqual({ action: "go" });
  });
});

describe("deriveReconcilerAction — chat commands defer until the roster settles", () => {
  it("returns null (no latch, no strip) for a chat command while agents are unsettled", () => {
    // Preserve the existing cold-load gate: nothing latched or executed while
    // agents.list is in flight.
    expect(deriveReconcilerAction("?agent=friday-id", null, false)).toBeNull();
    expect(deriveReconcilerAction("?panel=chat&agent=friday-id", null, false)).toBeNull();
  });

  it("the deferred command executes once agents settle (nothing was latched)", () => {
    // The shell re-runs the reconciler when isSettled flips true; because the
    // deferral latched nothing, the same search now produces the command.
    const d = deriveReconcilerAction("?agent=friday-id", null, true);
    expect(d?.command?.descriptor.panelId).toBe(MESSAGES_PANEL_ID);
    expect(d?.command?.descriptor.params).toEqual({ agentId: "friday-id" });
  });

  it("non-chat commands execute regardless of roster settlement", () => {
    const d = deriveReconcilerAction("?panel=teams-agents", null, false);
    expect(d?.command?.descriptor.panelId).toBe("teams-agents");
  });

  it("non-command URLs still latch while unsettled", () => {
    const d = deriveReconcilerAction("?utm_source=x", null, false);
    expect(d).not.toBeNull();
    expect(d!.command).toBeNull();
  });
});

describe("deriveReconcilerAction — one-shot strip ordering", () => {
  it("recomputes the latch AFTER stripping so the strip doesn't re-execute", () => {
    const d = deriveReconcilerAction("?panel=teams-issues&new=task", null);
    expect(d!.command?.oneShots).toEqual({ new: "task" });
    expect(d!.strippedSearch).toBe("panel=teams-issues");
    // One-shots ARE part of the signature, so the pre-strip signature differs…
    expect(commandSignature("?panel=teams-issues&new=task")).not.toBe(d!.nextSignature);
    // …and the latch must be the POST-strip signature…
    expect(d!.nextSignature).toBe(commandSignature("panel=teams-issues"));
    // …so the reconciler re-run triggered by the strip's URL churn is a no-op:
    expect(deriveReconcilerAction("panel=teams-issues", d!.nextSignature)).toBeNull();
  });

  it("leaves the latch at the plain signature when there is nothing to strip", () => {
    const d = deriveReconcilerAction("?panel=teams-agents", null);
    expect(d!.strippedSearch).toBeNull();
    expect(d!.nextSignature).toBe(commandSignature("?panel=teams-agents"));
  });
});

describe("deriveReconcilerAction — warm first mount reads live window.location", () => {
  // WorkspaceShell's reconciler effect passes window.location.search (the LIVE
  // URL), using searchParams only as the re-run trigger. onReady strips +
  // latches to the POST-strip signature synchronously; the effect then re-runs
  // and must see the already-stripped URL so it latches out. This models that:
  // executing a one-shot command latches the post-strip signature, and a re-run
  // reading the post-strip window.location value returns null (no double-open).
  it("latches out when re-run reads the post-strip URL against the post-strip latch", () => {
    const first = deriveReconcilerAction("?panel=teams-issues&new=task", null)!;
    expect(first.command?.oneShots).toEqual({ new: "task" });
    // onReady applied the strip → window.location.search is now the stripped
    // value, and lastApplied holds the post-strip signature.
    const liveSearch = first.strippedSearch!; // "panel=teams-issues"
    expect(deriveReconcilerAction(liveSearch, first.nextSignature)).toBeNull();
    // Contrast: reading the STALE pre-strip search (the old searchParams.toString
    // path) would NOT latch out — it re-executes the command:
    expect(deriveReconcilerAction("?panel=teams-issues&new=task", first.nextSignature)).not.toBeNull();
  });
});

describe("deriveReconcilerAction — cold-load one-shots apply once when the shell attaches", () => {
  // The shell runs nothing until it attaches, then runs the reconciler with the
  // latch reset to null; the strip's own URL churn re-runs it.
  it("?panel=teams-issues&new=task hands the one-shot to Work once", () => {
    const first = deriveReconcilerAction("?panel=teams-issues&new=task", null)!;
    expect(first.panelKey).toBe("teams-issues");
    expect(first.command?.oneShots).toEqual({ new: "task" });
    expect(first.strippedSearch).toBe("panel=teams-issues");
    expect(deriveReconcilerAction(first.strippedSearch!, first.nextSignature)).toBeNull();
  });

  it("a bare one-shot with no ?panel names no panel, so nothing opens", () => {
    const d = deriveReconcilerAction("?new=task", null)!;
    expect(d.command).toBeNull();
    expect(d.strippedSearch).toBeNull();
  });
});

describe("deriveReconcilerAction — billing returns are not commands", () => {
  it("never forms a command from checkout/subscription/credits/provider params", () => {
    const d = deriveReconcilerAction("?checkout=success&subscription=success", null);
    expect(d).not.toBeNull();
    expect(d!.command).toBeNull();
    expect(d!.strippedSearch).toBeNull();
    // useCheckoutReturn strips them → "" — same (empty) signature, no re-run:
    expect(deriveReconcilerAction("", d!.nextSignature)).toBeNull();
  });

  it("billing params riding along a command don't change its signature", () => {
    const d = deriveReconcilerAction("?panel=teams-agents&checkout=success", null);
    expect(d!.command?.descriptor.panelId).toBe("teams-agents");
    // after useCheckoutReturn strips the billing param the signature is unchanged:
    expect(deriveReconcilerAction("?panel=teams-agents", d!.nextSignature)).toBeNull();
  });
});

describe("isCommandTargetOpen (analytics newness check)", () => {
  it("detects an already-open panel by id", () => {
    const cmd = deriveReconcilerAction("?panel=teams-agents", null)!.command!;
    expect(isCommandTargetOpen(cmd, ["teams-agents"])).toBe(true);
    expect(isCommandTargetOpen(cmd, ["teams-issues"])).toBe(false);
  });

  it.each(["friday-id", "other-agent", "__default__"])("finds the %s chat command in Messages", (agentId) => {
    const cmd = deriveReconcilerAction(`?agent=${agentId}`, null)!.command!;
    expect(isCommandTargetOpen(cmd, [MESSAGES_PANEL_ID])).toBe(true);
    expect(isCommandTargetOpen(cmd, ["file-viewer"])).toBe(false);
  });
});

// The regression this exists for: every test above (and panelRegistry's,
// urlChannel's) checked ONE LINK of the deep-link chain, and the chain was
// broken at a seam none of them crossed — the reconciler pre-resolved the
// alias, so describePanel never saw the raw key and the successor opened on
// its default view. These walk the WHOLE chain the shell actually executes.
describe("retired-alias deep links keep their meaning end to end", () => {

  it("a stale ?panel=teams-dashboard (Home) opens the Activity place, not a dead view", () => {
    navigate("?panel=teams-dashboard");
    expect(getWorkspaceSnapshot()).toMatchObject({ place: "activity", activePanelId: "activity" });
  });

  it("every retired alias still lands on its successor's panel id", () => {
    for (const [alias, target] of Object.entries(RETIRED_PANEL_ALIASES)) {
      const d = navigate(`?panel=${alias}`);
      expect(d.panelKey).toBe(alias);
      const expectedId = describePanel(target, {})!.panelId;
      expect(getWorkspaceSnapshot().openPanelIds, `${alias} → ${expectedId}`).toContain(expectedId);
    }
  });
  it.each(["teams-inbox", "teams-approvals", "teams-activity", "teams-skills", "teams-costs"])(
    "?panel=%s opens the Activity place", (alias) => {
      navigate(`?panel=${alias}`);
      expect(getWorkspaceSnapshot().place).toBe("activity");
    });
});

// The dock is gone: every URL that opened a panel now names a place (the main view) or the
// one right pane.
describe("URL params map onto place + pane", () => {
  it.each([
    ["?panel=activity&focus=paperclip:approval:a1", "activity", "activity"],
    ["?panel=teams-issues&groupBy=status", "work", "teams-work"],
    ["?panel=teams-money&section=spend", "settings", "settings"],
    ["?panel=teams-agents&view=chart", "team", "teams-agents"],
    ["?panel=teams-search", "work", "teams-search"],
    ["?panel=teams-artifacts", "canvas", "canvas"],
  ])("%s → the %s place", (search, place, mainId) => {
    navigate(search);
    expect(getWorkspaceSnapshot()).toMatchObject({ place, activePanelId: mainId, pane: null });
  });

  it.each([
    ["?panel=teams-issue-detail&detailId=ADM-1", "teams-issue-detail:ADM-1", { teamsKey: "issue-detail", detailId: "ADM-1" }],
    ["?panel=teams-approval-detail&detailId=ap-1", "teams-approval-detail:ap-1", { teamsKey: "approval-detail", detailId: "ap-1" }],
    ["?panel=file-viewer&path=/notes.md&agent=a1", "file-viewer", { path: "/notes.md", agent: "a1" }],
    ["?panel=browser", "browser", {}],
    ["?panel=mail&item=mail:k1", "mail", { item: "mail:k1" }],
    ["?panel=agents&agent=a1", "control:agents", { controlKey: "agents", agentId: "a1" }],
    ["?panel=agent-inspector&agent=a1", "agent-inspector", { controlKey: "agent-inspector", agentId: "a1" }],
  ])("%s → the pane shows %s, the place stays", (search, paneId, params) => {
    navigate("?panel=teams-issues");
    navigate(search);
    const { pane, place } = getWorkspaceSnapshot();
    expect(place).toBe("work");
    expect(pane).toMatchObject({ panelId: paneId, params });
  });

  it("a URL naming a place and a pane restores both (a reload or a copied link)", () => {
    const d = navigate("?panel=teams-issues&agent=a-1&pane=teams-issue-detail&pane.detailId=ADM-1");
    expect(d.durableParams).toEqual({ agent: "a-1" }); // the pane's params stay off the place
    expect(getWorkspaceSnapshot()).toMatchObject({
      place: "work",
      main: { params: { teamsKey: "issues", agent: "a-1" } },
      pane: { panelId: "teams-issue-detail:ADM-1", params: { teamsKey: "issue-detail", detailId: "ADM-1" } },
    });
    // A file's own `agent` binding never collides with the place's agent filter.
    navigate("?panel=activity&pane=file-viewer&pane.path=/a.md&pane.agent=a-2");
    expect(getWorkspaceSnapshot()).toMatchObject({ place: "activity", main: { params: {} }, pane: { params: { path: "/a.md", agent: "a-2" } } });
  });

  it("a room and the pane beside it restore together (a reload of ?panel=chat&room=…&pane=browser)", () => {
    const d = navigate("?panel=chat&room=r-launch&pane=browser");
    expect(d.command?.descriptor.params).toEqual({ roomId: "r-launch" });
    openPanel("chat", { room: d.command!.descriptor.params.roomId }); // the shell's chat branch
    expect(getWorkspaceSnapshot()).toMatchObject({ place: "chat", chatParams: { roomId: "r-launch" }, pane: { panelId: "browser" } });
  });

  it("closing the pane clears only the pane part of a two-slot URL", () => {
    expect(clearPanelFromUrl("?panel=teams-issues&agent=a-1&pane=browser", "browser")).toBe("panel=teams-issues&agent=a-1");
    expect(clearPanelFromUrl("?panel=activity&pane=file-viewer&pane.path=%2Fa.md", "file-viewer")).toBe("panel=activity");
  });

  it("one pane at a time: a second open replaces the first", () => {
    navigate("?panel=file-viewer&path=/a.md");
    navigate("?panel=teams-issue-detail&detailId=ADM-2");
    expect(getWorkspaceSnapshot().pane?.panelId).toBe("teams-issue-detail:ADM-2");
    expect(getWorkspaceSnapshot().openPanelIds).not.toContain("file-viewer");
  });

  it("chat URLs name the Chat place, whose conversation is always mounted", () => {
    const d = deriveReconcilerAction("?panel=chat&room=r1&message=m1", null)!;
    expect(d.command?.descriptor).toMatchObject({ panelId: MESSAGES_PANEL_ID, params: { roomId: "r1", messageId: "m1" } });
    navigate("?panel=teams-issues");
    openPanel("chat", { room: "r1" });
    expect(getWorkspaceSnapshot()).toMatchObject({ place: "chat", main: null });
    expect(getWorkspaceSnapshot().chatParams).toMatchObject({ roomId: "r1" });
  });
});

describe("INVARIANT: panel open/focus writes zero history entries", () => {
  it("open, focus-existing, and reconciler-style executes never touch shellNav", () => {

    openPanel("teams-artifacts"); // add
    openPanel("teams-artifacts"); // focus existing

    // Reconciler-style execution: URL already carries the command → no pushUrl.
    const d = deriveReconcilerAction("?panel=teams-issues&new=task", null)!;
    openPanel(d.panelKey!, d.durableParams, { oneShots: d.command!.oneShots });

    expect(pushSearchParams).not.toHaveBeenCalled();
    expect(replaceSearchParams).not.toHaveBeenCalled();
  });

  it("only pushUrl:true writes — exactly one push, still zero replaces", () => {
    openPanel("activity", {}, { pushUrl: true });
    expect(pushSearchParams).toHaveBeenCalledTimes(1);
    expect(replaceSearchParams).not.toHaveBeenCalled();
  });
});
