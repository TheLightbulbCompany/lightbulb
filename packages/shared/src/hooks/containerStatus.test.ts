import { describe, expect, it } from "vitest";

import {
  classifyContainerStatus,
  parseBlockedPayload,
  type ContainerStatusBody,
} from "./containerStatus";

// Pure status-code + payload-shape parse, extracted from the two
// hand-written copies in apps/frontend/src/hooks/useProvisioningState.ts
// and apps/mobile/src/lib/containerStatus.ts (mobile onboarding-parity
// Slice 1). These tests pin the ONE parse both platforms now call.

describe("classifyContainerStatus", () => {
  describe("200 -> container", () => {
    it("passes the normal container row through RAW, untouched", () => {
      const body: ContainerStatusBody = {
        service_name: "openclaw-user123-abcd",
        status: "running",
        substatus: "gateway_healthy",
        created_at: "2026-07-01T00:00:00Z",
        updated_at: "2026-07-20T00:00:00Z",
        region: "us-east-1",
        last_error: null,
        last_error_at: null,
        phase: "ready",
        onboarded: true,
      };
      const result = classifyContainerStatus({ status: 200, body });
      expect(result).toEqual({ kind: "container", body });
      // Same object identity — a "passthrough" that reconstructs a new
      // object would still pass toEqual but would defeat the "raw body"
      // guarantee any downstream consumer (e.g. web's stepper) relies on.
      if (result.kind === "container") {
        expect(result.body).toBe(body);
      }
    });

    it("passes the synthetic no-row body through untouched (fresh owner, ensure_user_ready just kicked off)", () => {
      // Shape from routers/container.py's synthetic 200 (owner branch,
      // no container row yet): mostly nulls, phase="provisioning",
      // onboarded explicitly false.
      const body: ContainerStatusBody = {
        service_name: null,
        status: "provisioning",
        substatus: "ensure_user_ready",
        created_at: null,
        updated_at: null,
        region: "us-east-1",
        last_error: null,
        last_error_at: null,
        phase: "provisioning",
        onboarded: false,
      };
      const result = classifyContainerStatus({ status: 200, body });
      expect(result).toEqual({ kind: "container", body });
    });

    it("leaves phase='parked' in the body untouched — the classifier does not interpret it", () => {
      // Landmine from the plan's hazard audit: parked means "ready-
      // equivalent" on web and "keep polling, show a banner" on mobile.
      // That's a binding decision; the classifier must not collapse it.
      const body: ContainerStatusBody = {
        service_name: "x",
        status: "stopped",
        substatus: null,
        phase: "parked",
        parked: true,
      };
      const result = classifyContainerStatus({ status: 200, body });
      expect(result).toEqual({ kind: "container", body });
      if (result.kind === "container") {
        expect(result.body.phase).toBe("parked");
      }
    });

    it("passes a raw status of 'error' through on the body — the classifier does not special-case it either", () => {
      // The one behavior gap this slice closes lives in each binding
      // (web's ProvisioningStepper, mobile's containerStatus.ts), not
      // here — the classifier's job is only to hand the raw body over.
      const body: ContainerStatusBody = {
        service_name: "x",
        status: "error",
        substatus: null,
        last_error: "ECS task failed to start",
      };
      const result = classifyContainerStatus({ status: 200, body });
      expect(result).toEqual({ kind: "container", body });
    });
  });

  describe("202 -> team-pending", () => {
    it("extracts a string detail message", () => {
      const result = classifyContainerStatus({
        status: 202,
        body: { detail: "team workspace pending" },
      });
      expect(result).toEqual({ kind: "team-pending", detail: "team workspace pending" });
    });

    it("leaves detail undefined when the body has no string detail", () => {
      expect(classifyContainerStatus({ status: 202, body: {} })).toEqual({
        kind: "team-pending",
        detail: undefined,
      });
      expect(classifyContainerStatus({ status: 202, body: null })).toEqual({
        kind: "team-pending",
        detail: undefined,
      });
    });
  });

  describe("404 -> needs-signup", () => {
    it("classifies as needs-signup regardless of body content", () => {
      expect(
        classifyContainerStatus({ status: 404, body: { detail: "No container found" } }),
      ).toEqual({ kind: "needs-signup" });
    });
  });

  describe("402 -> blocked", () => {
    it("reads detail.blocked (the real FastAPI HTTPException wire shape)", () => {
      const result = classifyContainerStatus({
        status: 402,
        body: {
          detail: {
            blocked: {
              code: "credits_required",
              title: "Top up Claude credits",
              message: "Top up some Claude credits to start your Bedrock container.",
              action: {
                kind: "link",
                label: "Top up now",
                href: "/settings/billing#credits",
                admin_only: false,
              },
              owner_role: "admin",
            },
          },
        },
      });
      expect(result.kind).toBe("blocked");
      if (result.kind !== "blocked") throw new Error("expected blocked");
      expect(result.blocked.code).toBe("credits_required");
      expect(result.blocked.action.href).toBe("/settings/billing#credits");
      // S4-m8 follow-up: owner_role must survive the extraction intact —
      // ProvisionGate's adminOnly/ownerRole gating (Slice 4 fix) depends
      // on this field, not just action.admin_only.
      expect(result.blocked.owner_role).toBe("admin");
    });

    it("falls back to a top-level blocked payload (defensive — test fixtures / transport quirks)", () => {
      // This is the fallback mobile's old hand-rolled copy had silently
      // dropped; unifying on this classifier is what restores it there.
      const result = classifyContainerStatus({
        status: 402,
        body: {
          blocked: {
            code: "oauth_required",
            title: "Sign in with ChatGPT",
            message: "Complete the ChatGPT sign-in to start your container.",
            action: {
              kind: "link",
              label: "Sign in with ChatGPT",
              href: "/chat?provider=chatgpt_oauth",
              admin_only: true,
            },
            owner_role: "member",
          },
        },
      });
      expect(result.kind).toBe("blocked");
      if (result.kind !== "blocked") throw new Error("expected blocked");
      expect(result.blocked.code).toBe("oauth_required");
      expect(result.blocked.owner_role).toBe("member");
      expect(result.blocked.action.admin_only).toBe(true);
    });

    it("prefers detail.blocked over a top-level blocked when both are present", () => {
      const result = classifyContainerStatus({
        status: 402,
        body: {
          detail: { blocked: { code: "from-detail" } },
          blocked: { code: "from-top-level" },
        },
      });
      if (result.kind !== "blocked") throw new Error("expected blocked");
      expect(result.blocked.code).toBe("from-detail");
    });

    it("classifies as unrecognized when neither detail.blocked nor a top-level blocked is present", () => {
      // Defensive only — the backend always sends a blocked payload on 402
      // in practice (Gate.to_payload()). Callers must not synthesize a
      // fake blocked screen for this; unrecognized lets each binding apply
      // its own existing "real error" policy instead.
      expect(classifyContainerStatus({ status: 402, body: { detail: "not a gate payload" } })).toEqual(
        { kind: "unrecognized", status: 402 },
      );
      expect(classifyContainerStatus({ status: 402, body: null })).toEqual({
        kind: "unrecognized",
        status: 402,
      });
    });
  });

  describe("any other status -> unrecognized", () => {
    it("classifies a 500 as unrecognized, carrying the status", () => {
      expect(classifyContainerStatus({ status: 500, body: { detail: "boom" } })).toEqual({
        kind: "unrecognized",
        status: 500,
      });
    });

    it("never throws — even for a malformed 402 or a 5xx", () => {
      expect(() => classifyContainerStatus({ status: 402, body: undefined })).not.toThrow();
      expect(() => classifyContainerStatus({ status: 503, body: undefined })).not.toThrow();
    });
  });
});

describe("parseBlockedPayload", () => {
  it("reads detail.blocked", () => {
    const blocked = parseBlockedPayload({ detail: { blocked: { code: "x" } } });
    expect(blocked).toEqual({ code: "x" });
  });

  it("falls back to a top-level blocked", () => {
    const blocked = parseBlockedPayload({ blocked: { code: "y" } });
    expect(blocked).toEqual({ code: "y" });
  });

  it("returns null when neither shape is present", () => {
    expect(parseBlockedPayload({ detail: "plain string detail" })).toBeNull();
    expect(parseBlockedPayload({})).toBeNull();
    expect(parseBlockedPayload(null)).toBeNull();
    expect(parseBlockedPayload(undefined)).toBeNull();
    expect(parseBlockedPayload("not an object")).toBeNull();
  });

  it("preserves owner_role and the full action object intact", () => {
    const blocked = parseBlockedPayload({
      detail: {
        blocked: {
          code: "byo_provider_required",
          title: "Choose your BYO provider",
          message: "Choose Anthropic or OpenAI as your BYO provider to start your container.",
          action: { kind: "link", label: "Choose provider", href: "/chat?provider=byo_key", admin_only: false },
          owner_role: "admin",
        },
      },
    });
    expect(blocked).toEqual({
      code: "byo_provider_required",
      title: "Choose your BYO provider",
      message: "Choose Anthropic or OpenAI as your BYO provider to start your container.",
      action: { kind: "link", label: "Choose provider", href: "/chat?provider=byo_key", admin_only: false },
      owner_role: "admin",
    });
  });
});
