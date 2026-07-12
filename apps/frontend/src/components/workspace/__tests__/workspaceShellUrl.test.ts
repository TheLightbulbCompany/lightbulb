// Pins the WorkspaceShell reconciler's URL-logic ordering (pure level — no
// dockview render):
//   1. same signature twice → second call is a no-op (lastApplied latch)
//   2. the one-shot strip recomputes the latch so the strip's searchParams
//      churn doesn't re-execute the command
//   3. billing-return params never form a command
//   4. INVARIANT: the panel open/focus path performs ZERO history writes —
//      only pushUrl:true calls may write (spec §6: focus never touches the URL)
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { DockviewApi } from "dockview-react";

vi.mock("@/lib/shellNav", () => ({
  pushSearchParams: vi.fn(),
  replaceSearchParams: vi.fn(),
}));
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";

import { deriveReconcilerAction, isCommandTargetOpen } from "../workspaceShellReconciler";
import { commandSignature } from "../urlChannel";
import {
  attachDockviewApi,
  openPanel,
  _resetControllerForTests,
} from "../workspaceController";

// Minimal fake DockviewApi (trimmed copy of workspaceController.test.ts's) —
// just enough for openPanel's add-or-focus path.
type Handler<T> = (e: T) => void;
function makeFakeApi() {
  const panels = new Map<string, FakePanel>();
  const activeChange: Handler<{ panel: FakePanel | undefined }>[] = [];
  let active: FakePanel | undefined;
  function fire(panel: FakePanel | undefined) {
    active = panel;
    activeChange.forEach((h) => h({ panel }));
  }
  class FakePanel {
    api = {
      setActive: vi.fn(() => {
        fire(this);
      }),
      updateParameters: vi.fn(),
    };
    constructor(
      public id: string,
      public params: Record<string, string>,
    ) {}
  }
  const api = {
    getPanel: (id: string) => panels.get(id),
    addPanel: vi.fn((opts: { id: string; params?: Record<string, string> }) => {
      const p = new FakePanel(opts.id, opts.params ?? {});
      panels.set(opts.id, p);
      fire(p);
      return p;
    }),
    onDidActivePanelChange: (h: Handler<{ panel: FakePanel | undefined }>) => {
      activeChange.push(h);
      return { dispose: () => activeChange.splice(activeChange.indexOf(h), 1) };
    },
    onDidAddPanel: () => ({ dispose: vi.fn() }),
    onDidRemovePanel: () => ({ dispose: vi.fn() }),
    panels: [] as unknown[],
    groups: [] as unknown[],
  };
  Object.defineProperty(api, "panels", { get: () => [...panels.values()] });
  Object.defineProperty(api, "activePanel", { get: () => active });
  Object.defineProperty(api, "groups", { get: () => [{ activePanel: active }] });
  return api as unknown as DockviewApi;
}

beforeEach(() => {
  _resetControllerForTests();
  vi.clearAllMocks();
});

describe("deriveReconcilerAction — lastApplied latch", () => {
  it("executes a command once, then latches: same signature twice → no-op", () => {
    const first = deriveReconcilerAction("?panel=teams-inbox", null);
    expect(first).not.toBeNull();
    expect(first!.command?.descriptor.panelId).toBe("teams-inbox");
    expect(first!.panelKey).toBe("teams-inbox");
    const second = deriveReconcilerAction("?panel=teams-inbox", first!.nextSignature);
    expect(second).toBeNull();
  });

  it("latches even when the URL carries no command (so non-command churn is one-shot)", () => {
    const d = deriveReconcilerAction("?utm_source=x", null);
    expect(d).not.toBeNull();
    expect(d!.command).toBeNull();
    expect(deriveReconcilerAction("?utm_source=x", d!.nextSignature)).toBeNull();
  });

  it("resolves retired aliases into the panelKey handed to openPanel", () => {
    const d = deriveReconcilerAction("?panel=credits", null);
    expect(d!.panelKey).toBe("llm");
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
    const d = deriveReconcilerAction("?panel=teams-inbox", null);
    expect(d!.strippedSearch).toBeNull();
    expect(d!.nextSignature).toBe(commandSignature("?panel=teams-inbox"));
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
    const d = deriveReconcilerAction("?panel=teams-inbox&checkout=success", null);
    expect(d!.command?.descriptor.panelId).toBe("teams-inbox");
    // after useCheckoutReturn strips the billing param the signature is unchanged:
    expect(deriveReconcilerAction("?panel=teams-inbox", d!.nextSignature)).toBeNull();
  });
});

describe("isCommandTargetOpen (analytics newness check)", () => {
  it("detects an already-open panel by id", () => {
    const cmd = deriveReconcilerAction("?panel=teams-inbox", null)!.command!;
    expect(isCommandTargetOpen(cmd, ["teams-inbox"], () => undefined)).toBe(true);
    expect(isCommandTargetOpen(cmd, ["teams-issues"], () => undefined)).toBe(false);
  });

  it("unifies the chat sentinel with the concrete default agent", () => {
    const cmd = deriveReconcilerAction("?agent=friday-id", null)!.command!;
    expect(isCommandTargetOpen(cmd, ["chat:__default__"], () => "friday-id")).toBe(true);
    expect(isCommandTargetOpen(cmd, ["chat:__default__"], () => "other-id")).toBe(false);
    expect(isCommandTargetOpen(cmd, ["chat:friday-id"], () => undefined)).toBe(true);
  });
});

describe("INVARIANT: panel open/focus writes zero history entries", () => {
  it("open, focus-existing, and reconciler-style executes never touch shellNav", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);

    openPanel("teams-inbox"); // add
    openPanel("teams-inbox"); // focus existing

    // Reconciler-style execution: URL already carries the command → no pushUrl.
    const d = deriveReconcilerAction("?panel=teams-issues&new=task", null)!;
    openPanel(d.panelKey!, d.durableParams, { oneShots: d.command!.oneShots });

    expect(pushSearchParams).not.toHaveBeenCalled();
    expect(replaceSearchParams).not.toHaveBeenCalled();
  });

  it("only pushUrl:true writes — exactly one push, still zero replaces", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openPanel("teams-dashboard", {}, { pushUrl: true });
    expect(pushSearchParams).toHaveBeenCalledTimes(1);
    expect(replaceSearchParams).not.toHaveBeenCalled();
  });
});
