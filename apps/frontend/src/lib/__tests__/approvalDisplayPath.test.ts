import { describe, it, expect } from "vitest";
import { formatApprovalDisplayPath } from "../approvalDisplayPath";

describe("formatApprovalDisplayPath", () => {
  it("compacts a macOS home path to ~", () => {
    expect(formatApprovalDisplayPath("/Users/ray/projects/app")).toBe("~/projects/app");
  });
  it("compacts a Linux home path to ~", () => {
    expect(formatApprovalDisplayPath("/home/ray/projects/app")).toBe("~/projects/app");
  });
  it("compacts a Windows home path to ~ with forward slashes", () => {
    expect(formatApprovalDisplayPath("C:\\Users\\ray\\app")).toBe("~/app");
  });
  it("leaves a non-home absolute path untouched", () => {
    expect(formatApprovalDisplayPath("/etc/hosts")).toBe("/etc/hosts");
  });
  it("leaves a path with relative segments untouched (no false compaction)", () => {
    expect(formatApprovalDisplayPath("/Users/../etc/passwd")).toBe("/Users/../etc/passwd");
  });
  it("returns the trimmed value for empty/whitespace input", () => {
    expect(formatApprovalDisplayPath("   ")).toBe("");
  });
});
