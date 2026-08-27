import { describe, expect, it } from "vitest";
import { buildRecommendPath, buildSearchPath, resolveEnv } from "./env.js";

describe("resolveEnv", () => {
  const FULL = {
    LIGHTBULB_API_URL: "https://api.isol8.co/",
    LIGHTBULB_OWNER_ID: "user_abc",
    OPENCLAW_HOOK_TOKEN: "tok",
  };

  it("strips the trailing slash from the base url", () => {
    expect(resolveEnv(FULL).base).toBe("https://api.isol8.co");
  });

  it("names the missing variable so a container misconfig is diagnosable", () => {
    // A baked plugin is identical in every container; if the env is wrong the
    // error is the only signal anyone gets.
    expect(() => resolveEnv({ ...FULL, LIGHTBULB_OWNER_ID: undefined })).toThrow(/LIGHTBULB_OWNER_ID/);
    expect(() => resolveEnv({ ...FULL, LIGHTBULB_API_URL: undefined })).toThrow(/LIGHTBULB_API_URL/);
    expect(() => resolveEnv({ ...FULL, OPENCLAW_HOOK_TOKEN: undefined })).toThrow(
      /OPENCLAW_HOOK_TOKEN/,
    );
  });
});

describe("buildSearchPath", () => {
  it("requests the compact projection", () => {
    expect(buildSearchPath("content")).toContain("view=compact");
  });

  it("escapes a query that would otherwise inject another parameter", () => {
    // URLSearchParams form-encodes: '&' -> %26 (the part that matters), and
    // space -> '+', which Starlette decodes back to a space server-side.
    expect(buildSearchPath("a&b c")).toContain("q=a%26b+c");
  });
});

describe("buildRecommendPath", () => {
  it("puts the owner in the path, matching the hook-token route convention", () => {
    expect(buildRecommendPath("user_abc", ["pulse"], undefined)).toBe(
      "/api/v1/marketplace/u/user_abc/recommend?slugs=pulse",
    );
  });

  it("joins multiple slugs", () => {
    expect(buildRecommendPath("user_abc", ["pulse", "ora"], undefined)).toContain(
      "slugs=pulse%2Cora",
    );
  });

  it("carries issue_id when working a Paperclip task", () => {
    expect(buildRecommendPath("user_abc", ["pulse"], "issue_1")).toContain("issue_id=issue_1");
  });

  it("omits issue_id in webchat, where there is no task", () => {
    expect(buildRecommendPath("user_abc", ["pulse"], undefined)).not.toContain("issue_id");
  });

  it("encodes an owner id so it cannot break out of the path", () => {
    expect(buildRecommendPath("a/b", ["x"], undefined)).toContain("/u/a%2Fb/recommend");
  });
});
