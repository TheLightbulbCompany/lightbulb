import { expect, it, vi } from "vitest";
vi.mock("@/lib/analytics", () => ({ captureException: vi.fn() }));
import { viewerDiagnostic } from "../panels/dcvViewerError";

it("preserves the failing stage and exception while removing signed URLs", () => {
  const error = viewerDiagnostic("SDK import", {
    name: "TypeError",
    message: "Could not load https://aws.example/live?X-Amz-Signature=secret",
    stack: "connect@wss://aws.example/stream?token=private\nload@https://app.example/chunk.js:4:20",
  });
  expect(error.name).toBe("TypeError");
  expect(error.message).toContain("SDK import");
  expect(error.message).toContain("Could not load");
  expect(error.stack).not.toMatch(/secret|private|X-Amz|token=|aws\.example/);
  expect(error.stack).toContain("connect@");
});
