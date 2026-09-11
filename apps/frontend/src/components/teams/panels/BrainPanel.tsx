"use client";

// The company Brain — the memory-wiki vault the agents read and write on EFS
// (wiki/main/, served by routers/brain.py) — read like a wiki, not a
// directory: pages render as markdown, [[wikilinks]] navigate in-panel, the
// vault's own index.md (plugin-maintained on every wiki_apply compile) is the
// landing page — read as type cards over what changed lately once compile has
// written its managed block — and an explicit Edit toggle opens the raw-text
// editor (FileContentViewer: dirty tracking + scoped Cmd+S) for the audit/fix
// case.
// No file tree: navigation is links + breadcrumb + a name search; ASK hands
// the question to the agent, which owns wiki_search — no second index.
//
// The count is split honestly: "N filed" is exactly what wiki_search indexes
// (QUERY_DIRS); "M in inbox" is the owner drop-zone the plugin deliberately
// does NOT index. An owner who drops a file must see it counted apart from
// what ASK can reach, or the mechanism loses their trust.
//
// The vault, and nothing else: Dreams is a memory-core control surface and
// went back to being its own Settings panel (owner ruling 2026-09-08).

import * as React from "react";
import { useSWRConfig } from "swr";
import {
  BookOpen, Eye, MessageCircleQuestion, Pencil, RefreshCw, Trash2, TriangleAlert, Upload,
} from "lucide-react";
import {
  EditorialButton, PanelEmptyState, PanelMasthead, PanelNotice, PanelToolbar,
} from "@/components/teams/shell";
import { useAskCeo } from "@/hooks/useAskCeo";
import { useBrainTree, useBrainFile, useBrainMutations } from "@/hooks/useBrainFiles";
import type { FileInfo } from "@/hooks/useWorkspaceFiles";
import { notifyApiError } from "@/lib/notify";
import { formatBytes } from "@/lib/utils";
import { BrainFileList } from "./BrainFileList";
import { BrainLanding } from "./BrainLanding";
import { BrainPage } from "./BrainPage";
import {
  LANDING_PATH, askBrainDraft, fileInboxDraft, formatVaultDate, isInboxFile, parseIndexSections,
  indexOwnerProse,
  recentlyUpdated, searchVault, vaultCounts,
} from "./brainVault";

const CRUMB_BAR =
  "flex items-center gap-1 border-b border-[var(--edge)] px-3 py-1.5 text-xs text-[var(--midnight-muted)]";

const EMPTY_VAULT_PROMPT =
  "What do you know about our company so far? File anything durable into the brain.";

export interface BrainPanelProps {
  /** ?path deep link (registry-durable) — selects a page on mount. */
  initialPath?: string | null;
  /** The deep-linked page does not exist — a `?path=main/…` link from before
   *  the vault was re-rooted, or a page since deleted. The shell clears its
   *  durable `path` param (dockview params AND URL) so a persisted layout
   *  stops re-opening a dead page on every load. Fired once per stale path. */
  onPathUnavailable?: (path: string) => void;
}

export function BrainPanel({ initialPath, onPathUnavailable }: BrainPanelProps = {}) {
  const [selectedPath, setSelectedPath] = React.useState<string | null>(initialPath ?? null);
  const [editing, setEditing] = React.useState(false);
  const [editorDirty, setEditorDirty] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [view, setView] = React.useState<"page" | "inbox">("page");

  // A second `?path=` deep link into an already-open panel focuses it rather
  // than remounting (workspaceController's addOrFocus/updateParameters), so
  // the seed-only useState above never sees the new value on its own. Same
  // prev-prop-compare idiom as InboxPage.tsx's `?tab`. If the editor holds
  // unsaved edits, the open page wins — this runs during render, where a
  // confirm() is not an option.
  const [prevInitialPath, setPrevInitialPath] = React.useState(initialPath);
  // The once-per-bad-path guard for the stale-link clear below. Reset whenever
  // the deep-link param changes (including to null after a clear), so the same
  // dead path opened again into this panel is cleared again.
  const clearedRef = React.useRef<string | null>(null);
  if (initialPath !== prevInitialPath) {
    setPrevInitialPath(initialPath);
    clearedRef.current = null;
    if (initialPath && !editorDirty) {
      setSelectedPath(initialPath);
      setView("page");
      setEditing(false);
      // An active search would keep showing its results over the page asked for.
      setQuery("");
    }
  }

  const { files, isLoading: treeLoading, error: treeError, refresh } = useBrainTree();
  const counts = React.useMemo(() => vaultCounts(files), [files]);
  const hasLanding = files.some((f) => f.type === "file" && f.path === LANDING_PATH);
  // Land on the vault's own index.md: the plugin creates it on init and
  // rewrites its managed block (per-section counts + a wikilink to every
  // page, with real titles) on every wiki_apply compile.
  const pagePath = selectedPath ?? (hasLanding ? LANDING_PATH : null);
  const { file, isLoading: fileLoading, error: fileError, key: fileKey } = useBrainFile(
    view === "page" ? pagePath : null,
  );
  const { saveBrainFile, deleteBrainFile, uploadBrainFiles } = useBrainMutations();
  const { mutate: mutateCache } = useSWRConfig();

  // Stale deep link (the vault root moved from wiki/ to wiki/main/, so a
  // persisted `?path=main/…` 404s on every load) — or a rejected one
  // (_guard_brain_path answers 400/403 to a traversal-shaped path): clear
  // the durable param once, or a persisted layout reopens it on every
  // restore. Same three statuses BrainPage renders as a stale link. The
  // notice stays until the owner navigates.
  const fileStatus = (fileError as { status?: number } | null)?.status;
  const missing = fileStatus === 400 || fileStatus === 403 || fileStatus === 404;
  React.useEffect(() => {
    if (!missing || !pagePath || pagePath !== initialPath) return;
    if (clearedRef.current === pagePath) return;
    clearedRef.current = pagePath;
    onPathUnavailable?.(pagePath);
  }, [missing, pagePath, initialPath, onPathUnavailable]);

  const uploadInputRef = React.useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = React.useState(false);
  const [uploading, setUploading] = React.useState(false);

  // Owner drop-zone: dropped/picked files land in the vault's inbox/ so the
  // agents notice new material next time they check. Best-effort per drop —
  // a partial failure still refreshes the tree so whatever DID land shows up.
  // Guarded against re-entry so a second drop mid-upload never starts a
  // concurrent request (T3 review F7) — the drag/input handlers below also
  // skip while `uploading` so the drop-zone visibly goes inert.
  const handleUpload = React.useCallback(
    async (fileList: FileList | null) => {
      if (uploading) return;
      if (!fileList || fileList.length === 0) return;
      setUploading(true);
      try {
        await uploadBrainFiles(Array.from(fileList));
      } catch (err) {
        notifyApiError(err, "Couldn't upload files");
      } finally {
        setUploading(false);
        if (uploadInputRef.current) uploadInputRef.current.value = "";
      }
      void refresh();
    },
    [uploading, uploadBrainFiles, refresh],
  );

  // Nothing revalidates on its own — SwrProvider sets revalidateOnFocus:false
  // app-wide — so watching the CEO file a page needs an explicit ask.
  const confirmDiscardIfDirty = React.useCallback((): boolean => {
    if (!editorDirty) return true;
    return window.confirm("You have unsaved changes. Discard them?");
  }, [editorDirty]);

  // Nothing revalidates on its own — SwrProvider sets revalidateOnFocus:false
  // app-wide — so watching the CEO file a page needs an explicit ask. A fresh
  // file body re-seeds FileContentViewer, so a dirty draft is asked about
  // first, like every other navigation.
  const handleRefresh = React.useCallback(() => {
    if (!confirmDiscardIfDirty()) return;
    // A discarded draft ends the edit: if the server hands back identical
    // content the viewer's sync effect would not run and the stale draft would
    // linger with the dirty flag already cleared. Reading mode has no draft.
    setEditorDirty(false);
    setEditing(false);
    void refresh();
    if (fileKey) void mutateCache(fileKey);
  }, [confirmDiscardIfDirty, refresh, fileKey, mutateCache]);

  const handleSelect = React.useCallback(
    (path: string) => {
      if (!confirmDiscardIfDirty()) return;
      // FileContentViewer's dirty flag is lifted, its draft is not — an
      // accepted discard must reset the flag here or the next click re-asks.
      setEditorDirty(false);
      setEditing(false);
      setSelectedPath(path);
      setView("page");
      setQuery("");
    },
    [confirmDiscardIfDirty],
  );

  const showInbox = React.useCallback(() => {
    if (!confirmDiscardIfDirty()) return;
    setEditorDirty(false);
    setEditing(false);
    setQuery("");
    setView("inbox");
  }, [confirmDiscardIfDirty]);

  const toggleEdit = React.useCallback(() => {
    if (editing) {
      if (!confirmDiscardIfDirty()) return;
      setEditorDirty(false);
    }
    setEditing((v) => !v);
  }, [editing, confirmDiscardIfDirty]);

  const handleSave = React.useCallback(
    async (content: string) => {
      if (!pagePath) return;
      const fresh = await saveBrainFile(pagePath, content);
      // Merge the PUT's fresh stat + content into the file's own SWR entry (no
      // refetch) so the audit line and the rendered page go fresh instantly.
      if (fileKey) {
        void mutateCache(
          fileKey,
          (current?: FileInfo) =>
            current ? { ...current, size: fresh.size, modified_at: fresh.modified_at, content } : current,
          false,
        );
      }
      void refresh();
      // Back to reading. The viewer unmounts before its own post-save state
      // settles, so its last reported dirty=true must be reset here.
      setEditorDirty(false);
      setEditing(false);
    },
    [pagePath, saveBrainFile, refresh, fileKey, mutateCache],
  );

  const handleDelete = React.useCallback(async () => {
    if (!pagePath) return;
    if (!window.confirm(`Delete ${pagePath}? The agents will no longer see this page.`)) return;
    try {
      await deleteBrainFile(pagePath);
    } catch (err) {
      notifyApiError(err, "Couldn't delete the page");
      return;
    }
    setSelectedPath(null);
    setEditing(false);
    // The deleted file's content never clears from FileContentViewer's own
    // dirty-diff state (its sync effect only runs for a non-null file), so
    // without this a stale "unsaved changes" prompt fires on the next click.
    setEditorDirty(false);
    void refresh();
  }, [pagePath, deleteBrainFile, refresh]);

  // Every ASK exit hands the draft to the CEO agent's composer — desktop dock
  // or mobile shell, whichever is mounted (see useAskCeo).
  const ask = useAskCeo();

  const dropHandlers = {
    onDragOver: (e: React.DragEvent) => {
      e.preventDefault();
      if (uploading) return;
      setDragOver(true);
    },
    onDragLeave: () => setDragOver(false),
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      void handleUpload(e.dataTransfer.files);
    },
  };

  const isEmpty = !treeLoading && !treeError && files.length === 0;
  const searching = query.trim().length > 0;
  const results = React.useMemo(() => searchVault(files, query), [files, query]);
  const inboxFiles = React.useMemo(() => files.filter(isInboxFile), [files]);
  // The compiled landing. Only for the vault's own index.md, only in read
  // mode, and only when compile has actually written the managed block — a
  // scaffold vault ("- No compiled pages yet.") and a hand-written index both
  // parse to [], and fall through to the ordinary rendered page below.
  const indexSections = React.useMemo(
    () => (pagePath === LANDING_PATH && !editing ? parseIndexSections(file?.content ?? "") : []),
    [pagePath, editing, file],
  );
  const recent = React.useMemo(() => recentlyUpdated(files, 5), [files]);
  const isMarkdownPage = Boolean(file && !file.binary && file.content != null && file.name.endsWith(".md"));
  const showPage = !searching && view === "page" && Boolean(pagePath);

  // Breadcrumb: Index › <dir> › name. The dir segment opens the plugin's
  // per-section index.md when compile has written one.
  const crumbs = pagePath ? pagePath.split("/") : [];
  const sectionIndex = crumbs.length > 1 ? `${crumbs[0]}/index.md` : null;
  const sectionIndexPath =
    sectionIndex && files.some((f) => f.path === sectionIndex) ? sectionIndex : null;

  return (
    <div className="editorial-page h-full min-h-0 w-full p-4 sm:p-5 lg:p-6 flex flex-col gap-4">
      <PanelMasthead
        overline="Knowledge"
        title="Brain"
        // The masthead meta slot is mono by default (it usually carries a
        // stat line); this is a sentence, so it takes the UI face back. The
        // size · modified pair in `actions` keeps the mono.
        // Withheld until the tree is READ: `counts` is {0,0,0} while it
        // loads, and "0 filed" is a claim we have not earned yet. The count
        // is a button only when the inbox has something in it — at zero it
        // opens an empty list with nothing to do there.
        meta={
          !treeError && !treeLoading ? (
            <span className="font-sans">
              {counts.filed} filed ·{" "}
              {counts.inbox > 0 ? (
                <button
                  type="button"
                  onClick={showInbox}
                  className="underline decoration-dotted hover:text-[var(--midnight)]"
                >
                  {counts.inbox} in inbox awaiting filing
                </button>
              ) : (
                "0 in inbox awaiting filing"
              )}
            </span>
          ) : undefined
        }
        actions={
          showPage && file ? (
            <div className="flex items-center gap-2">
              <span className="text-xs tabular-nums font-mono text-[var(--midnight-muted)]">
                {formatBytes(file.size)} · {formatVaultDate(file.modified_at)}
              </span>
              {isMarkdownPage && (
                <button
                  type="button"
                  onClick={toggleEdit}
                  className="flex items-center gap-1 px-2 py-1 text-xs rounded-sm text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors"
                >
                  {editing ? <Eye className="h-3.5 w-3.5" /> : <Pencil className="h-3.5 w-3.5" />}
                  {editing ? "View" : "Edit"}
                </button>
              )}
              <button
                type="button"
                onClick={handleDelete}
                aria-label="Delete page"
                className="flex items-center gap-1 px-2 py-1 text-xs rounded-sm text-[var(--midnight-muted)] hover:text-err transition-colors"
              >
                <Trash2 className="h-3.5 w-3.5" />
                Delete
              </button>
            </div>
          ) : undefined
        }
      />

      {/* Hoisted so the empty-state and the notes pane share one file
          input — only one of those branches is ever mounted at a time.
          Disabled mid-upload (T3 review item 6/F7). */}
      <input
        ref={uploadInputRef}
        type="file"
        multiple
        className="sr-only"
        aria-label="Upload files to the brain inbox"
        disabled={uploading}
        onChange={(e) => void handleUpload(e.target.files)}
      />

      {treeError ? (
        <PanelEmptyState
          icon={TriangleAlert}
          headline="Couldn't load your brain."
          supporting="Something went wrong reaching your company's memory."
          action={{ label: "Retry", onClick: () => void refresh() }}
        />
      ) : isEmpty ? (
        // No vault at all: the plugin hasn't initialised (no agent has
        // touched the wiki yet). The likeliest place for the owner's very
        // first drop, so it keeps the drag/drop + picker affordance.
        <div
          data-testid="brain-empty-drop-zone"
          className={`flex-1 min-h-0 flex flex-col items-center justify-center rounded-sm border border-dashed transition-colors ${
            dragOver ? "border-[var(--midnight-muted)] bg-cream-deeper" : "border-transparent"
          }`}
          {...dropHandlers}
        >
          <PanelEmptyState
            icon={BookOpen}
            headline="No pages yet."
            supporting="Your company's brain fills as your agents learn."
            action={{ label: "Ask CEO", onClick: () => ask(askBrainDraft(query)) }}
          />
          <button
            type="button"
            onClick={() => uploadInputRef.current?.click()}
            disabled={uploading}
            className="text-xs text-[var(--midnight-muted)] underline hover:text-[var(--midnight)] disabled:opacity-50"
          >
            {uploading ? "Uploading…" : "or drop files here"}
          </button>
        </div>
      ) : (
        <>
          <PanelToolbar
            searchValue={query}
            // Searching short-circuits the pane, so it unmounts BrainPage
            // (and with it FileContentViewer's draft) exactly like a tab
            // switch does — it takes the same guard, unconditionally, like
            // every sibling handler: `editing` is NOT the same question as
            // "is an editor mounted". BrainPage routes any non-markdown
            // text file (.txt/.csv/.json — the backend serves those
            // binary:false) straight into the editable viewer with the
            // Edit toggle hidden, so a draft can be dirty while
            // `editing` is false. FileContentViewer also never reports
            // dirty=false on unmount, so an unguarded search would leave
            // `editorDirty` stuck true and fire a phantom prompt later.
            onSearchChange={(v) => {
              if (!confirmDiscardIfDirty()) return;
              setEditorDirty(false);
              setEditing(false);
              setQuery(v);
            }}
            searchPlaceholder="Find a page by name…"
            searchAriaLabel="Find a page by name"
            actions={
              <div className="flex items-center gap-2">
                <EditorialButton
                  variant="secondary"
                  size="sm"
                  onClick={() => ask(askBrainDraft(query))}
                >
                  <MessageCircleQuestion className="h-3.5 w-3.5" aria-hidden="true" />
                  Ask CEO
                </EditorialButton>
                <EditorialButton variant="ghost" size="sm" onClick={handleRefresh}>
                  <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                  Refresh
                </EditorialButton>
                <EditorialButton
                  variant="ghost"
                  size="sm"
                  onClick={() => uploadInputRef.current?.click()}
                  disabled={uploading}
                >
                  <Upload className="h-3.5 w-3.5" aria-hidden="true" />
                  {uploading ? "Uploading…" : "Add files"}
                </EditorialButton>
              </div>
            }
          />

          {!treeLoading && !searching && view === "page" && counts.inbox > 0 && (
            <PanelNotice
              state="partial"
              headline={`${counts.inbox} ${counts.inbox === 1 ? "file" : "files"} in the inbox awaiting filing.`}
              supporting="Your CEO's search can't see the inbox until a file is filed into the brain."
              prompt={fileInboxDraft(counts.inbox)}
              action={{
                label: counts.inbox === 1 ? "Ask CEO to file it" : "Ask CEO to file them",
                onClick: () => ask(fileInboxDraft(counts.inbox)),
              }}
            />
          )}
          {!treeLoading && !searching && view === "page" && counts.inbox === 0 && counts.knowledge === 0 && (
            // The plugin's first compile writes four reports/ dashboards
            // into an otherwise empty vault, so "Nothing filed yet." beside
            // an honest "4 filed" is every account's first post-compile
            // state. Name what those pages actually are instead.
            <PanelNotice
              state="zero"
              headline={
                counts.filed > 0 ? "Only the auto-written reports so far." : "Nothing filed yet."
              }
              supporting={
                counts.filed > 0
                  ? "Those dashboards are compiled from the vault itself — no one has filed anything into it yet. Drop a file here to give your CEO something to read."
                  : "Your CEO fills the brain as it works. Drop a file here to give it something to read."
              }
              prompt={EMPTY_VAULT_PROMPT}
              action={{
                label: "Ask CEO what we know",
                onClick: () => ask(EMPTY_VAULT_PROMPT),
              }}
            />
          )}

          <div
            data-testid="brain-notes-pane"
            className={`flex-1 min-h-0 overflow-y-auto rounded-sm border bg-cream transition-colors ${
              dragOver ? "border-dashed border-[var(--midnight-muted)] bg-cream-deeper" : "border-[var(--edge)]"
            }`}
            {...dropHandlers}
          >
            {treeLoading ? (
              <div className="p-6 font-editorial-serif text-xl italic text-[var(--midnight-muted)]">
                Loading…
              </div>
            ) : searching ? (
              <div className="p-3 flex flex-col gap-3">
                <p className="text-xs text-[var(--midnight-muted)]">
                  Matching page names only — contents aren&rsquo;t searched here.
                </p>
                <BrainFileList files={results} onSelect={handleSelect} emptyText="No page named that." />
                <EditorialButton
                  variant="secondary"
                  size="sm"
                  onClick={() => ask(askBrainDraft(query))}
                  className="self-start"
                >
                  Ask CEO to search the contents
                </EditorialButton>
              </div>
            ) : view === "inbox" ? (
              // Same breadcrumb the page view has: without it the inbox is
              // a one-way door — nothing else sets `view` back to "page".
              <div className="flex flex-col h-full">
                <nav aria-label="Breadcrumb" className={CRUMB_BAR}>
                  <button
                    type="button"
                    onClick={() => handleSelect(LANDING_PATH)}
                    className="hover:text-[var(--midnight)]"
                  >
                    Index
                  </button>
                  <span aria-hidden="true">›</span>
                  <span className="text-[var(--midnight)]">Inbox</span>
                </nav>
                <div className="flex-1 min-h-0 overflow-y-auto p-3">
                  <BrainFileList files={inboxFiles} onSelect={handleSelect} emptyText="The inbox is empty." />
                </div>
              </div>
            ) : pagePath ? (
              <div className="flex flex-col h-full">
                <nav aria-label="Breadcrumb" className={CRUMB_BAR}>
                  <button
                    type="button"
                    onClick={() => handleSelect(LANDING_PATH)}
                    className="hover:text-[var(--midnight)]"
                  >
                    Index
                  </button>
                  {/* EVERY directory, not just the first — the vault is a
                      tree and dropping the middle of a nested path names a
                      page that isn't where the breadcrumb says it is. Only
                      the top-level dir has an index.md to open. */}
                  {crumbs.slice(0, -1).map((seg, i) => (
                    <React.Fragment key={`${i}-${seg}`}>
                      <span aria-hidden="true">›</span>
                      {i === 0 && sectionIndexPath ? (
                        <button
                          type="button"
                          onClick={() => handleSelect(sectionIndexPath)}
                          className="hover:text-[var(--midnight)]"
                        >
                          {seg}
                        </button>
                      ) : (
                        <span>{seg}</span>
                      )}
                    </React.Fragment>
                  ))}
                  {pagePath !== LANDING_PATH && (
                    <>
                      <span aria-hidden="true">›</span>
                      <span className="text-[var(--midnight)]">{crumbs[crumbs.length - 1]}</span>
                    </>
                  )}
                </nav>
                <div className="flex-1 min-h-0 overflow-y-auto">
                  {/* A failed revalidation over a cached index falls through to
                      BrainPage, whose unavailable notice + Retry says so —
                      stale cards with no signal would be a lie. */}
                  {indexSections.length > 0 && !fileError ? (
                    <BrainLanding
                      sections={indexSections}
                      recent={recent}
                      files={files}
                      onNavigate={handleSelect}
                      intro={indexOwnerProse(file?.content ?? "")}
                    />
                  ) : (
                    <BrainPage
                      path={pagePath}
                      file={file}
                      isLoading={fileLoading}
                      error={fileError ?? null}
                      files={files}
                      editing={editing}
                      onNavigate={handleSelect}
                      onRetry={handleRefresh}
                      onSave={handleSave}
                      onDirtyChange={setEditorDirty}
                    />
                  )}
                </div>
              </div>
            ) : (
              // Vault exists but has no index.md (deleted, or init raced).
              // The plugin recreates it on the agent's next wiki call.
              <p className="p-6 text-center text-ui text-[var(--midnight-muted)]">
                Search for a page by name, or open the inbox.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
}
