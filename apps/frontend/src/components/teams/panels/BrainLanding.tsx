"use client";

// The landing the owner meets when compile has run: the vault's shape as type
// cards (one per `### Section` compile wrote into index.md's managed block)
// over what changed lately. Both are things a brain has and a folder does not
// — and the index.md this replaces carries no human prose, only `# Wiki Index`
// and `## Generated`, which the masthead ("Knowledge / Brain") already says.
//
// Pure props. Every number is derived by brainVault from the same tree and
// page content the rest of the panel reads; nothing here fetches.

import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { FileEntry } from "@/hooks/useWorkspaceFiles";
import { resolveVaultLink, type IndexSection } from "./brainVault";

export function BrainLanding({
  sections,
  recent,
  files,
  onNavigate,
}: {
  sections: IndexSection[];
  recent: FileEntry[];
  files: FileEntry[];
  onNavigate: (path: string) => void;
}) {
  return (
    <div className="flex flex-col gap-5 p-3">
      <div className="grid gap-3 sm:grid-cols-2">
        {sections.map((s) => {
          // `### Sources` is the heading over `sources/`; compile writes a
          // per-section index.md but only for sections it has pages for, so
          // Open appears only when the file is really there.
          const sectionIndex = `${s.section.toLowerCase()}/index.md`;
          const hasIndex = files.some((e) => e.type === "file" && e.path === sectionIndex);
          return (
            <section
              key={s.section}
              className="rounded-sm border border-[var(--edge)] bg-cream p-3"
            >
              <div className="flex items-baseline justify-between gap-2">
                <p className="label-caps">{s.section}</p>
                {/* The one mono number on the page — a count, like every other
                    stat in the shell's mastheads. */}
                <span className="font-mono text-xs tabular-nums text-[var(--midnight-muted)]">
                  {s.count}
                </span>
              </div>
              <ul className="mt-2 flex flex-col gap-1">
                {s.links.slice(0, 3).map((l) => (
                  <li key={l.target} className="min-w-0">
                    <button
                      type="button"
                      onClick={() => onNavigate(resolveVaultLink(l.target, files)?.path ?? l.target)}
                      className="block w-full truncate text-left text-ui text-[var(--midnight)] hover:underline"
                    >
                      {l.alias}
                    </button>
                  </li>
                ))}
              </ul>
              {hasIndex && (
                <button
                  type="button"
                  onClick={() => onNavigate(sectionIndex)}
                  className="mt-2 text-xs text-[var(--midnight-muted)] underline hover:text-[var(--midnight)]"
                >
                  Open
                </button>
              )}
            </section>
          );
        })}
      </div>

      {recent.length > 0 && (
        <section>
          <p className="label-caps mb-2">Recently updated</p>
          <ul className="divide-y divide-[var(--edge)]">
            {recent.map((e) => (
              <li key={e.path}>
                <button
                  type="button"
                  onClick={() => onNavigate(e.path)}
                  className="flex w-full items-baseline justify-between gap-3 px-2 py-2 text-left transition-colors hover:bg-[var(--cream-deep)]"
                >
                  <span className="truncate text-ui text-[var(--midnight)]">
                    {e.name.replace(/\.md$/, "")}
                  </span>
                  <span className="shrink-0 text-xs text-[var(--midnight-muted)]">
                    {e.path.slice(0, e.path.indexOf("/"))} ·{" "}
                    {/* modified_at is st_mtime — POSIX seconds, not ms. */}
                    {timeAgo(new Date(e.modified_at * 1000))}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
