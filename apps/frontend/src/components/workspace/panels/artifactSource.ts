export type ArtifactSource =
  | { src: "attachment"; room: string; id: string; name: string | null; type: string | null }
  | { src: "paperclip"; kind: "document"; issue: string; key: string; ident: string | null }
  | { src: "paperclip"; kind: "attachment" | "work_product"; issue: string; id: string; name: string | null; type: string | null }
  | { src: "file"; agent: string; path: string };

/** The pane's URL params → a typed source, or null when a required part is missing. */
export function parseArtifactSource(p: Record<string, string>): ArtifactSource | null {
  if (p.src === "attachment" && p.room && p.id) return { src: "attachment", room: p.room, id: p.id, name: p.name ?? null, type: p.type ?? null };
  if (p.src === "paperclip" && p.kind === "document" && p.issue && p.key) return { src: "paperclip", kind: "document", issue: p.issue, key: p.key, ident: p.ident ?? null };
  if (p.src === "paperclip" && (p.kind === "attachment" || p.kind === "work_product") && p.issue && p.id) return { src: "paperclip", kind: p.kind, issue: p.issue, id: p.id, name: p.name ?? null, type: p.type ?? null };
  if (p.src === "file" && p.agent && p.path) return { src: "file", agent: p.agent, path: p.path };
  return null;
}

export const ARTIFACT_PARAM_KEYS = ["src", "room", "id", "kind", "issue", "key", "ident", "name", "type", "agent", "path"] as const;
