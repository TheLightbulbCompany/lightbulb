import { describe, expect, it } from "vitest";
import { parseArtifactSource } from "../artifactSource";

describe("parseArtifactSource", () => {
  it("accepts each source shape and rejects a missing part", () => {
    expect(parseArtifactSource({ src: "attachment", room: "r", id: "a" })).toEqual({ src: "attachment", room: "r", id: "a", name: null, type: null });
    expect(parseArtifactSource({ src: "paperclip", kind: "document", issue: "i", key: "plan" })).toBeNull();
    expect(parseArtifactSource({ src: "paperclip", kind: "work_product", issue: "i", id: "w", type: "application/pdf" })).toMatchObject({ kind: "work_product", type: "application/pdf" });
    expect(parseArtifactSource({ src: "file", agent: "ag", path: "work/out.csv" })).toMatchObject({ src: "file" });
    expect(parseArtifactSource({ src: "attachment", room: "r" })).toBeNull();
    expect(parseArtifactSource({ src: "nope" })).toBeNull();
  });
});
