"use client";

import type { FileEntry } from "@/hooks/useWorkspaceFiles";
import { formatBytes } from "@/lib/utils";
import { formatVaultDate } from "./brainVault";

/** The one list the Brain shows: search results and the inbox. Rows are
 *  vault paths (the plugin's slugs read fine as names) with the audit pair.
 *
 *  It filters nothing — what a list shows is the caller's question, not this
 *  component's (`searchVault` already drops plumbing; the inbox is whole by
 *  definition). Note `isPlumbing` is dot-paths only: it does NOT hide
 *  `index.md` / `AGENTS.md` / `WIKI.md`, which `isFiledPage` excludes from
 *  COUNTS but which are real pages an owner may well want to open. */
export function BrainFileList({
  files,
  onSelect,
  emptyText,
}: {
  files: FileEntry[];
  onSelect: (path: string) => void;
  emptyText: string;
}) {
  if (files.length === 0) {
    return <p className="py-6 text-center text-ui text-[var(--midnight-muted)]">{emptyText}</p>;
  }
  return (
    <ul className="divide-y divide-[var(--edge)]">
      {files.map((f) => (
        <li key={f.path}>
          <button
            type="button"
            onClick={() => onSelect(f.path)}
            className="flex w-full items-baseline justify-between gap-3 px-2 py-2 text-left transition-colors hover:bg-[var(--cream-deep)]"
          >
            <span className="truncate text-ui text-[var(--midnight)]">{f.path}</span>
            <span className="shrink-0 text-xs tabular-nums font-mono text-[var(--midnight-muted)]">
              {f.size != null ? `${formatBytes(f.size)} · ` : ""}
              {formatVaultDate(f.modified_at)}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
