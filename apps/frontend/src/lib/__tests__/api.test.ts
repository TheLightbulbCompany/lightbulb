import { describe, expect, it } from "vitest";

import { ApiError } from "../api";

describe("ApiError", () => {
  it("captures the status code and parsed body", () => {
    const err = new ApiError(402, { blocked: { code: "credits_required" } });
    expect(err.status).toBe(402);
    expect(err.body).toEqual({ blocked: { code: "credits_required" } });
    expect(err.name).toBe("ApiError");
  });

  it("uses a generic default message when none provided", () => {
    const err = new ApiError(503, null);
    expect(err.message).toBe("Something went wrong");
  });

  it("uses the provided message when given", () => {
    const err = new ApiError(404, null, "Resource not found");
    expect(err.message).toBe("Resource not found");
  });

  it("captures the backend request id when provided", () => {
    const err = new ApiError(500, null, "boom", "req_abc");
    expect(err.requestId).toBe("req_abc");
  });

  it("leaves requestId undefined when omitted", () => {
    const err = new ApiError(500, null, "boom");
    expect(err.requestId).toBeUndefined();
  });

  it("is throwable and instanceof Error", () => {
    expect(() => {
      throw new ApiError(400, { error: "bad" });
    }).toThrow(ApiError);
    try {
      throw new ApiError(400, { error: "bad" });
    } catch (error) {
      expect(error).toBeInstanceOf(Error);
      expect(error).toBeInstanceOf(ApiError);
    }
  });
});
