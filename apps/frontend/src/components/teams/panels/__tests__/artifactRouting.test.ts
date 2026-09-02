import { describe, it, expect } from "vitest";
import {
  artifactKind,
  artifactRawId,
  documentKeyFromArtifact,
  deletePathForArtifact,
  issueListPathForArtifact,
  artifactContentPath,
  sortGroupsByUpdated,
  sortArtifactsByUpdated,
  bucketByRecency,
} from "../artifactRouting";
import type {
  CompanyArtifact,
  CompanyArtifactGroup,
} from "@/components/teams/shared/types";

function artifact(over: Partial<CompanyArtifact> & { id: string }): CompanyArtifact {
  return {
    source: "document",
    mediaKind: "document",
    title: "t",
    updatedAt: "2026-01-01T00:00:00Z",
    href: "",
    issue: { id: "iss_1", identifier: "ISO-1", title: "Task" },
    ...over,
  };
}

describe("artifactRouting", () => {
  it("classifies the typed id prefix", () => {
    expect(artifactKind("document:d1")).toBe("document");
    expect(artifactKind("work_product:w1")).toBe("work_product");
    expect(artifactKind("attachment:a1")).toBe("attachment");
    expect(artifactKind("bogus:x")).toBeNull();
    expect(artifactKind("noprefix")).toBeNull();
  });

  it("extracts the raw upstream id after the prefix", () => {
    expect(artifactRawId("work_product:11111111-2222")).toBe("11111111-2222");
    expect(artifactRawId("attachment:att1")).toBe("att1");
  });

  it("parses a document key out of the href fragment (dashes allowed)", () => {
    expect(
      documentKeyFromArtifact(artifact({ id: "document:d1", href: "/ACME/issues/ISO-1#document-plan" })),
    ).toBe("plan");
    expect(
      documentKeyFromArtifact(artifact({ id: "document:d1", href: "/ACME/issues/ISO-1#document-my-doc-2" })),
    ).toBe("my-doc-2");
    expect(documentKeyFromArtifact(artifact({ id: "document:d1", href: "" }))).toBeNull();
    expect(
      documentKeyFromArtifact(artifact({ id: "document:d1", href: "/x#attachment-att1" })),
    ).toBeNull();
  });

  it("routes each prefix to the correct BFF DELETE path", () => {
    expect(
      deletePathForArtifact(artifact({ id: "document:d1", href: "/ACME/issues/ISO-1#document-plan" })),
    ).toBe("/issues/iss_1/documents/plan");
    expect(deletePathForArtifact(artifact({ id: "work_product:wp1" }))).toBe(
      "/work-products/wp1",
    );
    expect(deletePathForArtifact(artifact({ id: "attachment:att1" }))).toBe(
      "/issues/iss_1/attachments/att1",
    );
  });

  it("returns null when a document delete can't be resolved (no key)", () => {
    expect(deletePathForArtifact(artifact({ id: "document:d1", href: "" }))).toBeNull();
    expect(deletePathForArtifact(artifact({ id: "bogus:x" }))).toBeNull();
  });

  it("routes each prefix to the issue-scoped LIST path for cache invalidation", () => {
    // A document delete only needs the issue (not the key) to invalidate the
    // issue-detail documents list.
    expect(
      issueListPathForArtifact(artifact({ id: "document:d1", href: "" })),
    ).toBe("/issues/iss_1/documents");
    expect(issueListPathForArtifact(artifact({ id: "work_product:wp1" }))).toBe(
      "/issues/iss_1/work-products",
    );
    expect(issueListPathForArtifact(artifact({ id: "attachment:att1" }))).toBe(
      "/issues/iss_1/attachments",
    );
    // No issue → nothing to invalidate; unrecognized prefix → null.
    expect(
      issueListPathForArtifact(artifact({ id: "work_product:wp1", issue: undefined })),
    ).toBeNull();
    expect(issueListPathForArtifact(artifact({ id: "bogus:x" }))).toBeNull();
  });

  it("resolves the BFF content route for downloadable, attachment-backed artifacts", () => {
    // A direct attachment carries its id in its own typed id.
    expect(artifactContentPath(artifact({ id: "attachment:att1" }))).toBe(
      "/attachments/att1/content",
    );
    // A work-product's attachment id lives only in its upstream open/download
    // path; strip the `/api` prefix and the `?download=1` query.
    expect(
      artifactContentPath(
        artifact({
          id: "work_product:wp1",
          downloadPath: "/api/attachments/att9/content?download=1",
        }),
      ),
    ).toBe("/attachments/att9/content");
    expect(
      artifactContentPath(
        artifact({ id: "work_product:wp1", openPath: "/api/attachments/att9/content" }),
      ),
    ).toBe("/attachments/att9/content");
    // Documents (and anything without an attachment path) have no content route.
    expect(
      artifactContentPath(artifact({ id: "document:d1", href: "/x#document-plan" })),
    ).toBeNull();
    expect(artifactContentPath(artifact({ id: "work_product:wp1" }))).toBeNull();
  });

  it("sorts artifacts newest/oldest by updatedAt without mutating the input", () => {
    const a = artifact({ id: "attachment:a", updatedAt: "2026-01-01T00:00:00Z" });
    const b = artifact({ id: "attachment:b", updatedAt: "2026-02-01T00:00:00Z" });
    const list = [a, b];
    expect(sortArtifactsByUpdated(list, "newest").map((x) => x.id)).toEqual([
      "attachment:b",
      "attachment:a",
    ]);
    expect(sortArtifactsByUpdated(list, "oldest").map((x) => x.id)).toEqual([
      "attachment:a",
      "attachment:b",
    ]);
    expect(list.map((x) => x.id)).toEqual(["attachment:a", "attachment:b"]);
  });

  it("sorts groups newest/oldest by updatedAt without mutating the input", () => {
    const g = (id: string, updatedAt: string): CompanyArtifactGroup => ({
      id,
      groupBy: "task",
      issue: { id, identifier: id, title: id },
      title: id,
      count: 1,
      mediaKinds: [],
      previewArtifacts: [],
      updatedAt,
      href: "",
    });
    const groups = [g("a", "2026-01-01T00:00:00Z"), g("b", "2026-02-01T00:00:00Z")];
    expect(sortGroupsByUpdated(groups, "newest").map((x) => x.id)).toEqual(["b", "a"]);
    expect(sortGroupsByUpdated(groups, "oldest").map((x) => x.id)).toEqual(["a", "b"]);
    // input untouched
    expect(groups.map((x) => x.id)).toEqual(["a", "b"]);
  });

  it("buckets artifacts into Past 7 days / Past 30 days / Older by updatedAt", () => {
    const now = Date.parse("2026-09-01T12:00:00Z");
    const mk = (id: string, updatedAt: string) => artifact({ id, updatedAt });
    const out = bucketByRecency(
      [
        mk("old", "2026-06-01T00:00:00Z"),
        mk("wk", "2026-08-30T00:00:00Z"),
        mk("mo", "2026-08-10T00:00:00Z"),
        mk("bad", "not-a-date"),
      ],
      now,
    );
    expect(out.map((b) => [b.label, b.artifacts.map((a) => a.id)])).toEqual([
      ["Past 7 days", ["wk"]],
      ["Past 30 days", ["mo"]],
      ["Older", ["old", "bad"]],
    ]);
  });
});
