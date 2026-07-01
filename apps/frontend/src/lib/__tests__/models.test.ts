import { describe, it, expect } from "vitest";
import {
  buildModelRef,
  isReasoningModel,
  providerLabelFor,
  resolveSessionModelRef,
  type ModelEntry,
} from "@/lib/models";

const entry = (over: Partial<ModelEntry>): ModelEntry => ({
  id: "gpt-4o",
  provider: "openai",
  ...over,
});

describe("buildModelRef", () => {
  it("joins provider and id verbatim without splitting on dots", () => {
    // Bedrock application-inference-profile ARN — the '.'-split in
    // ModelSelector.extractProvider mangled this; we must not touch it.
    const id =
      "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/abc.def";
    expect(buildModelRef("amazon-bedrock", id)).toBe(`amazon-bedrock/${id}`);
  });
  it("builds a plain openai ref", () => {
    expect(buildModelRef("openai", "gpt-5")).toBe("openai/gpt-5");
  });
});

describe("isReasoningModel", () => {
  it("honors an explicit reasoning flag over heuristics", () => {
    expect(isReasoningModel(entry({ id: "gpt-4o", reasoning: true }))).toBe(true);
    expect(isReasoningModel(entry({ id: "o3", reasoning: false }))).toBe(false);
  });
  it("detects reasoning families by id when no flag is present", () => {
    expect(isReasoningModel(entry({ id: "o3-mini" }))).toBe(true);
    expect(isReasoningModel(entry({ id: "gpt-5" }))).toBe(true);
    expect(
      isReasoningModel(entry({ id: "claude-sonnet-4-20250514", provider: "anthropic" })),
    ).toBe(true);
  });
  it("returns false for non-reasoning models", () => {
    expect(isReasoningModel(entry({ id: "gpt-4o" }))).toBe(false);
    expect(isReasoningModel(entry({ id: "claude-3-5-haiku", provider: "anthropic" }))).toBe(false);
  });
});

describe("providerLabelFor", () => {
  it("uses the entry provider field, never a dot-split", () => {
    expect(
      providerLabelFor(entry({ id: "us.anthropic.claude-x", provider: "amazon-bedrock" })),
    ).toBe("amazon-bedrock");
  });
});

describe("resolveSessionModelRef", () => {
  it("returns a model already stored as a full provider/id ref unchanged", () => {
    expect(resolveSessionModelRef("openai/gpt-5", "openai")).toBe("openai/gpt-5");
  });
  it("joins a bare id with a separately-reported provider", () => {
    expect(resolveSessionModelRef("gpt-5", "openai")).toBe("openai/gpt-5");
  });
  it("does not double-prefix a Bedrock ARN that already carries its provider", () => {
    // ARNs contain internal '/', so a bare '/' test would misfire — the
    // startsWith(provider + '/') guard keeps this a no-op.
    const ref =
      "amazon-bedrock/arn:aws:bedrock:us-east-1:1:application-inference-profile/abc.def";
    expect(resolveSessionModelRef(ref, "amazon-bedrock")).toBe(ref);
  });
  it("joins a bare Bedrock ARN id (with internal slash) to its provider", () => {
    const arn = "arn:aws:bedrock:us-east-1:1:application-inference-profile/abc.def";
    expect(resolveSessionModelRef(arn, "amazon-bedrock")).toBe(`amazon-bedrock/${arn}`);
  });
  it("returns the model as-is when no provider is reported", () => {
    expect(resolveSessionModelRef("openai/gpt-5")).toBe("openai/gpt-5");
  });
  it("returns empty string when no model is persisted (pill falls back to default)", () => {
    expect(resolveSessionModelRef(undefined, "openai")).toBe("");
    expect(resolveSessionModelRef("")).toBe("");
  });
});
