// apps/frontend/src/components/teams/routines/RoutineHistoryTab.tsx

// Ported from upstream Paperclip's `RoutineHistoryTab`
// (paperclip/ui/src/components/RoutineHistoryTab.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). The revision list (8 newest + "Show N older…"),
// the read-only preview (structured fields with "differs from current",
// description, trigger + variable snapshots), the historical-preview banner,
// the restore confirmation, and the compare modal (field changes, env, trigger
// and description line diffs) are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - react-query routinesApi.listRevisions / restoreRevision -> useTeamsApi
//     read of /teams/routines/{id}/revisions and POST …/restore; the restore
//     goes through the BFF's paid-plan gate, so a 402 renders the upgrade toast
//   - the unsaved-edits ConflictBanner and `isEditDirty` gating are not ported:
//     our routine edit is a modal dialog, so nothing is dirty while this tab
//     is visible
//   - the restore dialog's "Change summary" input is dropped: upstream's
//     restore route ignores the body at this tag, so the field saved nothing
//   - company secrets are not read (`secretsApi` stays out of the BFF), so a
//     secret_ref env binding is named by a short secret id, not its name
//   - pushToast -> notifySuccess / notifyApiError; relativeTime -> timeAgo;
//     MarkdownBody -> MarkdownView; shadcn Dialog/Button/Badge -> the editorial
//     shell; emerald/amber/red palette -> the --ok/--warn/--err brand tokens
//   - DiffTable renders our lineDiff's extra `truncated` notice row

"use client";

import { useEffect, useMemo, useState } from "react";
import { History as HistoryIcon, RotateCcw, Search } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { notifyApiError, notifyConversionGate, notifySuccess } from "@/lib/notify";
import { buildLineDiff, type DiffRow } from "@/components/teams/issues/lib/lineDiff";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { EditorialButton } from "@/components/teams/shell/EditorialButton";
import { EditorialDialog } from "@/components/teams/shell/EditorialDialog";
import type {
  RestoreRoutineRevisionResponse,
  RoutineDetail,
  RoutineRevision,
  RoutineRevisionSnapshotRoutine,
  RoutineRevisionSnapshotTrigger,
  RoutineVariable,
} from "@/components/teams/shared/types";

type AgentLookup = Map<string, { id: string; name: string }>;
type ProjectLookup = Map<string, { id: string; name: string }>;

type Props = {
  routine: RoutineDetail;
  agents: AgentLookup;
  projects: ProjectLookup;
  onRestored?: (response: RestoreRoutineRevisionResponse) => void;
};

const EMPTY_SNAPSHOT: RoutineRevisionSnapshotRoutine = { title: "" };

function snapshotRoutine(revision: RoutineRevision): RoutineRevisionSnapshotRoutine {
  return revision.snapshot?.routine ?? EMPTY_SNAPSHOT;
}

function snapshotTriggers(revision: RoutineRevision): RoutineRevisionSnapshotTrigger[] {
  return revision.snapshot?.triggers ?? [];
}

function revNumber(revision: RoutineRevision): number {
  return revision.revisionNumber ?? 0;
}

const CAPS = "text-caption font-medium uppercase tracking-wide text-[var(--midnight-muted)]";

function Tag({ tone, children }: { tone?: "warn" | "ok" | "err"; children: React.ReactNode }) {
  const color = tone ? `var(--${tone})` : "var(--midnight-muted)";
  return (
    <span
      className="label-caps inline-flex items-center rounded-sm border px-1.5 text-2xs"
      style={{ borderColor: color, color }}
    >
      {children}
    </span>
  );
}

export function RoutineHistoryTab({ routine, agents, projects, onRestored }: Props) {
  const { read, post } = useTeamsApi();
  const revisionsQuery = read<{ revisions: RoutineRevision[] }>(`/routines/${routine.id}/revisions`);
  const [selectedRevisionId, setSelectedRevisionId] = useState<string | null>(null);
  const [highlightedRevisionId, setHighlightedRevisionId] = useState<string | null>(null);
  const [showOlder, setShowOlder] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [diffOpen, setDiffOpen] = useState(false);
  const [restorePending, setRestorePending] = useState(false);

  const revisions = useMemo(() => revisionsQuery.data?.revisions ?? [], [revisionsQuery.data]);
  const sortedRevisions = useMemo(
    () => [...revisions].sort((a, b) => revNumber(b) - revNumber(a)),
    [revisions],
  );
  const latestRevisionId = routine.latestRevisionId ?? sortedRevisions[0]?.id ?? null;
  const currentRevision = useMemo(
    () => sortedRevisions.find((r) => r.id === latestRevisionId) ?? sortedRevisions[0] ?? null,
    [sortedRevisions, latestRevisionId],
  );

  useEffect(() => {
    if (selectedRevisionId === null && currentRevision) {
      setSelectedRevisionId(currentRevision.id);
    }
  }, [currentRevision, selectedRevisionId]);

  const selectedRevision = useMemo(
    () => sortedRevisions.find((r) => r.id === selectedRevisionId) ?? null,
    [sortedRevisions, selectedRevisionId],
  );
  const isHistoricalSelected = !!selectedRevision && selectedRevision.id !== latestRevisionId;
  const visibleRevisions = useMemo(() => {
    if (showOlder || sortedRevisions.length <= 8) return sortedRevisions;
    return sortedRevisions.slice(0, 8);
  }, [sortedRevisions, showOlder]);

  const restore = async (revisionId: string) => {
    setRestorePending(true);
    try {
      const data = await post<RestoreRoutineRevisionResponse>(
        `/routines/${routine.id}/revisions/${revisionId}/restore`,
        {},
      );
      const newRevision = data?.revision ?? null;
      // Upstream's toast body ("Trigger enabled state was restored…") is
      // dropped: notifySuccess is title-only, and any new webhook secret is
      // shown by the host's one-time dialog (onRestored).
      notifySuccess(
        `Restored revision ${data?.restoredFromRevisionNumber ?? "?"} as revision ${newRevision?.revisionNumber ?? "?"}`,
      );
      if (data) onRestored?.(data);
      setConfirmOpen(false);
      if (newRevision) {
        setSelectedRevisionId(newRevision.id);
        setHighlightedRevisionId(newRevision.id);
        window.setTimeout(() => {
          setHighlightedRevisionId((current) => (current === newRevision.id ? null : current));
        }, 3000);
      }
      await revisionsQuery.mutate?.();
    } catch (error) {
      if (notifyConversionGate(error) === null) {
        notifyApiError(error, "Failed to restore revision");
      }
    } finally {
      setRestorePending(false);
    }
  };

  const openRestoreConfirm = () => {
    if (!selectedRevision || !isHistoricalSelected) return;
    setConfirmOpen(true);
  };

  if (revisionsQuery.isLoading && !revisionsQuery.data) {
    return (
      <p className="text-ui italic" style={{ color: "var(--midnight-muted)" }}>
        Loading revisions…
      </p>
    );
  }

  if (revisionsQuery.error) {
    return (
      <div
        role="alert"
        className="flex flex-col gap-3 rounded-md border p-4"
        style={{ borderColor: "var(--edge)", borderLeft: "2px solid var(--err)" }}
      >
        <div>
          <p className="text-ui font-medium">Could not load revisions</p>
          <p className="text-caption text-[var(--midnight-muted)]">
            {revisionsQuery.error instanceof Error
              ? revisionsQuery.error.message
              : "Unknown error loading revisions."}
          </p>
        </div>
        <div>
          <EditorialButton variant="secondary" size="sm" onClick={() => revisionsQuery.mutate?.()}>
            Retry
          </EditorialButton>
        </div>
      </div>
    );
  }

  const onlyBootstrapRevision = revisions.length <= 1;

  return (
    <div className="grid gap-5 @3xl:grid-cols-[220px_minmax(0,1fr)]">
      <RevisionList
        revisions={visibleRevisions}
        latestRevisionId={latestRevisionId}
        selectedRevisionId={selectedRevisionId}
        highlightedRevisionId={highlightedRevisionId}
        totalRevisions={sortedRevisions.length}
        onSelect={setSelectedRevisionId}
        onShowOlder={() => setShowOlder(true)}
        showOlder={showOlder}
      />
      <div className="flex min-w-0 flex-col gap-4">
        {onlyBootstrapRevision ? (
          <div className="flex flex-col items-center gap-2 py-6 text-center">
            <HistoryIcon size={20} aria-hidden="true" className="text-[var(--midnight-muted)]" />
            <p className="text-ui font-medium">No edits yet</p>
            <p className="text-caption text-[var(--midnight-muted)]">
              Revision 1 is the only history this routine has. Saving an edit creates the first
              additional revision.
            </p>
          </div>
        ) : (
          selectedRevision && (
            <>
              {isHistoricalSelected && currentRevision && (
                <HistoricalPreviewBanner
                  revisionNumber={revNumber(selectedRevision)}
                  nextRevisionNumber={revNumber(currentRevision) + 1}
                  onReturn={() => setSelectedRevisionId(currentRevision.id)}
                  onRestore={openRestoreConfirm}
                  pending={restorePending}
                />
              )}
              <RevisionPreview
                revision={selectedRevision}
                currentRevision={currentRevision}
                isHistorical={isHistoricalSelected}
                agents={agents}
                projects={projects}
                onCompare={() => setDiffOpen(true)}
                onRestore={openRestoreConfirm}
                restorePending={restorePending}
                highlighted={highlightedRevisionId === selectedRevision.id}
              />
            </>
          )
        )}
      </div>

      {selectedRevision && currentRevision && (
        <RestoreConfirmDialog
          open={confirmOpen}
          onOpenChange={setConfirmOpen}
          target={selectedRevision}
          currentRevisionNumber={revNumber(currentRevision)}
          onConfirm={() => void restore(selectedRevision.id)}
          pending={restorePending}
          recreatedWebhookLabels={collectWebhookTriggerDifferences(selectedRevision, currentRevision)}
          envDiffCounts={summarizeEnvDiffCounts(
            snapshotRoutine(currentRevision).env ?? null,
            snapshotRoutine(selectedRevision).env ?? null,
          )}
        />
      )}

      {currentRevision && selectedRevision && diffOpen && (
        <RoutineRevisionDiffModal
          onOpenChange={setDiffOpen}
          revisions={sortedRevisions}
          initialOldRevisionId={selectedRevision.id}
          initialNewRevisionId={currentRevision.id}
          agents={agents}
          projects={projects}
          onRestore={(rev) => {
            setSelectedRevisionId(rev.id);
            setDiffOpen(false);
            setConfirmOpen(true);
          }}
        />
      )}
    </div>
  );
}

function HistoricalPreviewBanner({
  revisionNumber,
  nextRevisionNumber,
  onReturn,
  onRestore,
  pending,
}: {
  revisionNumber: number;
  nextRevisionNumber: number;
  onReturn: () => void;
  onRestore: () => void;
  pending: boolean;
}) {
  return (
    <div className="rounded-md border px-4 py-3" style={{ borderColor: "var(--warn)" }}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex flex-col gap-1">
          <p className="text-ui font-medium" style={{ color: "var(--warn)" }}>
            Viewing revision {revisionNumber} (read-only)
          </p>
          <p className="text-caption text-[var(--midnight-muted)]">
            Restoring this revision creates a new revision {nextRevisionNumber} with the same
            content. History stays append-only.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <EditorialButton variant="secondary" size="sm" onClick={onReturn} disabled={pending}>
            Return to current
          </EditorialButton>
          <EditorialButton variant="primary" size="sm" onClick={onRestore} disabled={pending}>
            <RotateCcw size={14} aria-hidden="true" />
            Restore as new revision
          </EditorialButton>
        </div>
      </div>
    </div>
  );
}

function RevisionList({
  revisions,
  latestRevisionId,
  selectedRevisionId,
  highlightedRevisionId,
  totalRevisions,
  onSelect,
  onShowOlder,
  showOlder,
}: {
  revisions: RoutineRevision[];
  latestRevisionId: string | null;
  selectedRevisionId: string | null;
  highlightedRevisionId: string | null;
  totalRevisions: number;
  onSelect: (revisionId: string) => void;
  onShowOlder: () => void;
  showOlder: boolean;
}) {
  return (
    <aside className="flex flex-col gap-1">
      <header className="flex items-center justify-between pb-2">
        <p className={CAPS}>Revisions</p>
        <span className="text-caption text-[var(--midnight-muted)]">{totalRevisions} total</span>
      </header>
      {revisions.map((revision) => {
        const isSelected = revision.id === selectedRevisionId;
        const isCurrent = revision.id === latestRevisionId;
        const isHistorical = !isCurrent;
        const isHighlighted = revision.id === highlightedRevisionId;
        const borderColor = isHighlighted
          ? "var(--ok)"
          : isSelected && isHistorical
            ? "var(--warn)"
            : "var(--edge)";
        return (
          <button
            key={revision.id}
            type="button"
            onClick={() => onSelect(revision.id)}
            aria-pressed={isSelected}
            className="w-full rounded-md border px-3 py-2 text-left transition-colors hover:bg-[var(--cream-deep)]"
            style={{
              borderColor,
              background: isSelected ? "var(--cream-deep)" : undefined,
            }}
            data-testid={`revision-row-${revision.revisionNumber}`}
          >
            <div className="flex items-center gap-2 text-ui font-medium">
              <span>rev {revision.revisionNumber}</span>
              {isCurrent && <Tag>Current</Tag>}
              {revision.restoredFromRevisionId && <Tag tone="warn">Restored</Tag>}
            </div>
            <div className="truncate text-caption text-[var(--midnight-muted)]">
              {revision.createdAt ? timeAgo(revision.createdAt) : "—"} • {getActorLabel(revision)}
              {revision.changeSummary ? ` • ${revision.changeSummary}` : ""}
            </div>
          </button>
        );
      })}
      {totalRevisions > revisions.length && !showOlder && (
        <EditorialButton variant="ghost" size="sm" className="w-full" onClick={onShowOlder}>
          Show {totalRevisions - revisions.length} older…
        </EditorialButton>
      )}
    </aside>
  );
}

function RevisionPreview({
  revision,
  currentRevision,
  isHistorical,
  agents,
  projects,
  onCompare,
  onRestore,
  restorePending,
  highlighted,
}: {
  revision: RoutineRevision;
  currentRevision: RoutineRevision | null;
  isHistorical: boolean;
  agents: AgentLookup;
  projects: ProjectLookup;
  onCompare: () => void;
  onRestore: () => void;
  restorePending: boolean;
  highlighted: boolean;
}) {
  const snapshot = snapshotRoutine(revision);
  const triggers = snapshotTriggers(revision);
  const variables = snapshot.variables ?? [];
  const currentSnapshot = currentRevision ? snapshotRoutine(currentRevision) : null;
  const restoreLabel = "Restore this revision";
  const cardStyle = { borderColor: highlighted ? "var(--ok)" : "var(--edge)" };
  const cardClass = "rounded-md border transition-colors duration-1000";

  const envDiffers =
    !!currentSnapshot &&
    JSON.stringify(normalizeEnv(currentSnapshot.env ?? null)) !==
      JSON.stringify(normalizeEnv(snapshot.env ?? null));
  const fieldRows: Array<{ key: string; label: string; value: string; differs: boolean }> = [
    {
      key: "title",
      label: "Title",
      value: snapshot.title,
      differs: !!currentSnapshot && currentSnapshot.title !== snapshot.title,
    },
    {
      key: "priority",
      label: "Priority",
      value: snapshot.priority ?? "",
      differs: !!currentSnapshot && currentSnapshot.priority !== snapshot.priority,
    },
    {
      key: "status",
      label: "Status",
      value: snapshot.status ?? "",
      differs: !!currentSnapshot && currentSnapshot.status !== snapshot.status,
    },
    {
      key: "assigneeAgentId",
      label: "Default agent",
      value: resolveAgentName(snapshot.assigneeAgentId ?? null, agents),
      differs: !!currentSnapshot && currentSnapshot.assigneeAgentId !== snapshot.assigneeAgentId,
    },
    {
      key: "projectId",
      label: "Project",
      value: resolveProjectName(snapshot.projectId ?? null, projects),
      differs: !!currentSnapshot && currentSnapshot.projectId !== snapshot.projectId,
    },
    {
      key: "concurrencyPolicy",
      label: "Concurrency",
      value: (snapshot.concurrencyPolicy ?? "").replaceAll("_", " "),
      differs: !!currentSnapshot && currentSnapshot.concurrencyPolicy !== snapshot.concurrencyPolicy,
    },
    {
      key: "catchUpPolicy",
      label: "Catch-up",
      value: (snapshot.catchUpPolicy ?? "").replaceAll("_", " "),
      differs: !!currentSnapshot && currentSnapshot.catchUpPolicy !== snapshot.catchUpPolicy,
    },
    {
      key: "env",
      label: "Env",
      value: summarizeEnv(snapshot.env ?? null),
      differs: envDiffers,
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <header className={`${cardClass} flex flex-col gap-2 p-4`} style={cardStyle}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 flex-col gap-1">
            <p className="text-ui font-medium">rev {revision.revisionNumber}</p>
            <p className="truncate text-caption text-[var(--midnight-muted)]">
              Saved {revision.createdAt ? timeAgo(revision.createdAt) : "—"} by{" "}
              {getActorLabel(revision)}
              {revision.changeSummary ? ` · ${revision.changeSummary}` : ""}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <EditorialButton variant="secondary" size="sm" onClick={onCompare}>
              <Search size={14} aria-hidden="true" />
              Compare with current
            </EditorialButton>
            <EditorialButton
              variant="primary"
              size="sm"
              onClick={onRestore}
              disabled={!isHistorical || restorePending}
              aria-label={restoreLabel}
            >
              <RotateCcw size={14} aria-hidden="true" />
              {restoreLabel}
            </EditorialButton>
          </div>
        </div>
      </header>

      <div className={`${cardClass} p-3`} style={cardStyle}>
        <p className={`${CAPS} pb-2`}>Structured fields</p>
        <div className="grid gap-3 md:grid-cols-2">
          {fieldRows.map((row) => (
            <div key={row.key} className="flex flex-col gap-1 p-2">
              <p className="text-caption uppercase tracking-wide text-[var(--midnight-muted)]">
                {row.label}
              </p>
              <p className="text-ui">
                {row.value || <span className="text-[var(--midnight-muted)]">—</span>}
                {row.differs && (
                  <span className="ml-2">
                    <Tag tone="warn">differs from current</Tag>
                  </span>
                )}
              </p>
            </div>
          ))}
        </div>
      </div>

      <div className={`${cardClass} flex flex-col gap-2 p-3`} style={cardStyle}>
        <p className={CAPS}>Description</p>
        <div className="rounded-md p-3 text-ui leading-7">
          {snapshot.description ? (
            <MarkdownView>{snapshot.description}</MarkdownView>
          ) : (
            <span className="text-[var(--midnight-muted)]">No description</span>
          )}
        </div>
      </div>

      <div className={`${cardClass} flex flex-col gap-2 p-3`} style={cardStyle}>
        <p className={CAPS}>Triggers ({triggers.length})</p>
        {triggers.length === 0 ? (
          <p className="text-ui text-[var(--midnight-muted)]">No triggers in this revision.</p>
        ) : (
          <ul>
            {triggers.map((trigger) => (
              <li
                key={trigger.id}
                className="flex flex-wrap items-center gap-2 py-2 text-ui"
                style={{ borderBottom: "1px solid var(--edge)" }}
              >
                <Tag>{trigger.kind}</Tag>
                <span className="font-medium">{trigger.label ?? trigger.kind}</span>
                <span className="text-caption text-[var(--midnight-muted)]">
                  {summarizeTriggerSnapshot(trigger)}
                </span>
                <span
                  className="ml-auto text-caption"
                  style={{ color: trigger.enabled ? "var(--ok)" : "var(--midnight-muted)" }}
                >
                  {trigger.enabled ? "enabled" : "disabled"}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="text-caption text-[var(--midnight-muted)]">
          Webhook secrets are not stored in revisions. If a restored webhook trigger needs
          re-creation, Paperclip mints fresh secret material at restore time.
        </p>
      </div>

      {variables.length > 0 && (
        <div className={`${cardClass} flex flex-col gap-2 p-3`} style={cardStyle}>
          <p className={CAPS}>Variables ({variables.length})</p>
          <ul>
            {variables.map((variable) => (
              <li
                key={variable.name}
                className="flex items-center justify-between py-2 text-ui"
                style={{ borderBottom: "1px solid var(--edge)" }}
              >
                <span className="font-mono text-caption">{variable.name}</span>
                <span className="text-caption text-[var(--midnight-muted)]">
                  default: {formatVariableDefault(variable)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function RestoreConfirmDialog({
  open,
  onOpenChange,
  target,
  currentRevisionNumber,
  onConfirm,
  pending,
  recreatedWebhookLabels,
  envDiffCounts,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  target: RoutineRevision;
  currentRevisionNumber: number;
  onConfirm: () => void;
  pending: boolean;
  recreatedWebhookLabels: string[];
  envDiffCounts: EnvDiffCounts;
}) {
  const newRevisionNumber = currentRevisionNumber + 1;
  const bullet = (color: string) => (
    <span
      aria-hidden="true"
      className="mt-1.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full"
      style={{ background: color }}
    />
  );
  return (
    <EditorialDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Restore revision ${target.revisionNumber}?`}
      description={`This creates a new revision ${newRevisionNumber} with the same content as revision ${target.revisionNumber}. Revisions ${target.revisionNumber}–${currentRevisionNumber} stay in history and are not modified.`}
      footer={
        <>
          <EditorialButton
            variant="secondary"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={pending}
          >
            Cancel
          </EditorialButton>
          <EditorialButton variant="primary" size="sm" onClick={onConfirm} disabled={pending}>
            <RotateCcw size={14} aria-hidden="true" />
            {pending ? "Restoring…" : `Restore as revision ${newRevisionNumber}`}
          </EditorialButton>
        </>
      }
    >
      <ul className="flex flex-col gap-2 text-ui">
        <li className="flex items-start gap-2">
          {bullet("var(--ok)")}
          Routine field values, variables, and schedule cron will revert.
        </li>
        {envDiffCounts.total > 0 && (
          <li className="flex items-start gap-2">
            {bullet("var(--ok)")}
            Routine secrets will revert: {formatEnvDiffCounts(envDiffCounts)}.
          </li>
        )}
        <li className="flex items-start gap-2">
          {bullet("var(--ok)")}
          Previous run history is preserved.
        </li>
        {recreatedWebhookLabels.map((label) => (
          <li key={label} className="flex items-start gap-2" style={{ color: "var(--warn)" }}>
            {bullet("var(--warn)")}
            The webhook trigger {label} will be recreated with a new URL and secret. Paperclip will
            show the secret once after restore — copy it before closing.
          </li>
        ))}
      </ul>
    </EditorialDialog>
  );
}

function RoutineRevisionDiffModal({
  onOpenChange,
  revisions,
  initialOldRevisionId,
  initialNewRevisionId,
  agents,
  projects,
  onRestore,
}: {
  onOpenChange: (open: boolean) => void;
  revisions: RoutineRevision[];
  initialOldRevisionId: string;
  initialNewRevisionId: string;
  agents: AgentLookup;
  projects: ProjectLookup;
  onRestore: (revision: RoutineRevision) => void;
}) {
  // Mounted only while open (ours), so the initial ids seed state directly
  // instead of upstream's reset-on-open effect.
  const [leftId, setLeftId] = useState<string>(initialOldRevisionId);
  const [rightId, setRightId] = useState<string>(initialNewRevisionId);

  const left = revisions.find((r) => r.id === leftId) ?? null;
  const right = revisions.find((r) => r.id === rightId) ?? null;
  const fieldChanges = useMemo(
    () => (left && right ? computeFieldChanges(left, right, agents, projects) : []),
    [left, right, agents, projects],
  );
  const descriptionDiff = useMemo<DiffRow[]>(
    () =>
      left && right
        ? buildLineDiff(snapshotRoutine(left).description ?? "", snapshotRoutine(right).description ?? "")
        : [],
    [left, right],
  );
  const newest = revisions[0] ?? null;
  const leftIsHistorical = !!left && !!newest && left.id !== newest.id;

  return (
    <EditorialDialog
      open
      onOpenChange={onOpenChange}
      title="Compare routine revisions"
      className="sm:max-w-[90vw]"
      footer={
        <>
          <EditorialButton
            variant="secondary"
            size="sm"
            className="mr-auto"
            onClick={() => onOpenChange(false)}
          >
            Close
          </EditorialButton>
          {leftIsHistorical && left && (
            <EditorialButton variant="primary" size="sm" onClick={() => onRestore(left)}>
              <RotateCcw size={14} aria-hidden="true" />
              Restore rev {left.revisionNumber} as new revision
            </EditorialButton>
          )}
        </>
      }
    >
      <div className="flex flex-wrap items-center gap-3">
        <RevisionPicker label="Old" value={leftId} onChange={setLeftId} revisions={revisions} tone="err" />
        <RevisionPicker label="New" value={rightId} onChange={setRightId} revisions={revisions} tone="ok" />
      </div>
      <section className="flex flex-col gap-2">
        <p className={CAPS}>Field changes</p>
        {fieldChanges.length === 0 ? (
          <p className="text-ui text-[var(--midnight-muted)]">No structural field changes.</p>
        ) : (
          <table className="w-full overflow-hidden rounded-md border text-ui" style={{ borderColor: "var(--edge)" }}>
            <thead>
              <tr className="text-caption uppercase tracking-wide text-[var(--midnight-muted)]">
                <th className="px-3 py-2 text-left">Field</th>
                <th className="px-3 py-2 text-left">Old value</th>
                <th className="px-3 py-2 text-left">New value</th>
              </tr>
            </thead>
            <tbody>
              {fieldChanges.map((change) => (
                <tr key={change.field} style={{ borderTop: "1px solid var(--edge)" }}>
                  <td className="px-3 py-2 align-top text-caption font-medium">{change.field}</td>
                  <td className="px-3 py-2 align-top text-caption" style={{ color: "var(--err)" }}>
                    {change.oldValue ?? "—"}
                  </td>
                  <td className="px-3 py-2 align-top text-caption" style={{ color: "var(--ok)" }}>
                    {change.newValue ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="flex flex-col gap-2">
        <p className={CAPS}>Description diff</p>
        <DiffTable rows={descriptionDiff} />
      </section>
    </EditorialDialog>
  );
}

function RevisionPicker({
  label,
  value,
  onChange,
  revisions,
  tone,
}: {
  label: string;
  value: string;
  onChange: (id: string) => void;
  revisions: RoutineRevision[];
  tone: "err" | "ok";
}) {
  return (
    <label className="flex items-center gap-2">
      <Tag tone={tone}>{label}</Tag>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-label={`${label} revision`}
        className="h-8 min-w-48 rounded-md border bg-[var(--cream)] px-2 text-caption"
        style={{ borderColor: "var(--edge)" }}
      >
        {revisions.map((revision) => (
          <option key={revision.id} value={revision.id}>
            rev {revision.revisionNumber} — {revision.createdAt ? timeAgo(revision.createdAt) : "—"}
            {revision.changeSummary ? ` • ${revision.changeSummary}` : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

function DiffTable({ rows }: { rows: DiffRow[] }) {
  if (rows.length === 0) {
    return <p className="text-ui text-[var(--midnight-muted)]">No description on either revision.</p>;
  }
  if (rows.every((row) => row.kind === "context")) {
    return <p className="text-ui text-[var(--midnight-muted)]">Descriptions are identical.</p>;
  }
  const colorByKind: Record<DiffRow["kind"], string | undefined> = {
    context: undefined,
    removed: "var(--err)",
    added: "var(--ok)",
    truncated: "var(--midnight-muted)",
  };
  const markerByKind: Record<DiffRow["kind"], string> = {
    context: " ",
    removed: "-",
    added: "+",
    truncated: " ",
  };
  const gridCols = "grid grid-cols-[3rem_3rem_1.5rem_minmax(0,1fr)]";
  return (
    <div
      className="overflow-hidden rounded-md border font-mono text-caption leading-6"
      style={{ borderColor: "var(--edge)" }}
    >
      <div
        className={`${gridCols} px-3 py-2 text-caption uppercase tracking-wide text-[var(--midnight-muted)]`}
        style={{ borderBottom: "1px solid var(--edge)" }}
      >
        <span>Old</span>
        <span>New</span>
        <span />
        <span>Content</span>
      </div>
      {rows.map((row, index) =>
        row.kind === "truncated" ? (
          <div key={`truncated-${index}`} className="px-3 py-1 italic" style={{ color: colorByKind.truncated }}>
            {row.text}
          </div>
        ) : (
          <div
            key={`${row.kind}-${index}-${row.oldLineNumber ?? "x"}-${row.newLineNumber ?? "x"}`}
            className={`${gridCols} px-3`}
            style={{ color: colorByKind[row.kind], borderBottom: "1px solid var(--edge)" }}
          >
            <span className="select-none pr-3 text-right text-[var(--midnight-muted)]">
              {row.oldLineNumber ?? ""}
            </span>
            <span className="select-none px-3 text-right text-[var(--midnight-muted)]">
              {row.newLineNumber ?? ""}
            </span>
            <span className="select-none px-1 text-center">{markerByKind[row.kind]}</span>
            <pre className="overflow-x-auto whitespace-pre-wrap break-words px-3 py-0 text-inherit">
              {row.text.length > 0 ? row.text : " "}
            </pre>
          </div>
        ),
      )}
    </div>
  );
}

function getActorLabel(revision: RoutineRevision): string {
  if (revision.createdByUserId) return "board";
  if (revision.createdByAgentId) return "agent";
  return "system";
}

function resolveAgentName(agentId: string | null, lookup: AgentLookup) {
  if (!agentId) return "Unassigned";
  return lookup.get(agentId)?.name ?? agentId;
}

function resolveProjectName(projectId: string | null, lookup: ProjectLookup) {
  if (!projectId) return "No project";
  return lookup.get(projectId)?.name ?? projectId;
}

function summarizeTriggerSnapshot(trigger: RoutineRevisionSnapshotTrigger): string {
  if (trigger.kind === "schedule") {
    return [trigger.cronExpression, trigger.timezone].filter(Boolean).join(" · ");
  }
  if (trigger.kind === "webhook") {
    const replay = trigger.replayWindowSec != null ? `replay ${trigger.replayWindowSec}s` : "";
    return [trigger.signingMode, replay].filter(Boolean).join(" · ");
  }
  return "API";
}

function formatVariableDefault(variable: RoutineVariable): string {
  if (variable.defaultValue == null) return "—";
  return String(variable.defaultValue);
}

function collectWebhookTriggerDifferences(target: RoutineRevision, current: RoutineRevision): string[] {
  const currentIds = new Set(snapshotTriggers(current).map((t) => t.id));
  return snapshotTriggers(target)
    .filter((trigger) => trigger.kind === "webhook" && !currentIds.has(trigger.id))
    .map((trigger) => trigger.label ?? "webhook");
}

function describeSnapshotField(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

type FieldChange = { field: string; oldValue: string | null; newValue: string | null };

function computeFieldChanges(
  left: RoutineRevision,
  right: RoutineRevision,
  agents: AgentLookup,
  projects: ProjectLookup,
): FieldChange[] {
  const oldRoutine = snapshotRoutine(left);
  const newRoutine = snapshotRoutine(right);
  const changes: FieldChange[] = [];
  const compareScalar = (label: string, oldVal: unknown, newVal: unknown) => {
    if (JSON.stringify(oldVal) !== JSON.stringify(newVal)) {
      changes.push({ field: label, oldValue: describeSnapshotField(oldVal), newValue: describeSnapshotField(newVal) });
    }
  };
  compareScalar("Title", oldRoutine.title, newRoutine.title);
  compareScalar("Priority", oldRoutine.priority, newRoutine.priority);
  compareScalar(
    "Default agent",
    resolveAgentName(oldRoutine.assigneeAgentId ?? null, agents),
    resolveAgentName(newRoutine.assigneeAgentId ?? null, agents),
  );
  compareScalar(
    "Project",
    resolveProjectName(oldRoutine.projectId ?? null, projects),
    resolveProjectName(newRoutine.projectId ?? null, projects),
  );
  compareScalar("Concurrency", oldRoutine.concurrencyPolicy, newRoutine.concurrencyPolicy);
  compareScalar("Catch-up", oldRoutine.catchUpPolicy, newRoutine.catchUpPolicy);
  compareScalar("Status", oldRoutine.status, newRoutine.status);
  const oldVars = oldRoutine.variables ?? [];
  const newVars = newRoutine.variables ?? [];
  if (JSON.stringify(oldVars) !== JSON.stringify(newVars)) {
    changes.push({ field: "Variables", oldValue: summarizeVariables(oldVars), newValue: summarizeVariables(newVars) });
  }
  compareEnv(oldRoutine.env ?? null, newRoutine.env ?? null, changes);
  compareTriggers(snapshotTriggers(left), snapshotTriggers(right), changes);
  return changes;
}

type EnvBinding = unknown;
type SecretRef = { type: "secret_ref"; secretId: string; version?: number | "latest" };

function normalizeEnv(env: Record<string, EnvBinding> | null): Record<string, EnvBinding> {
  return env ?? {};
}

function asSecretRef(binding: EnvBinding): SecretRef | null {
  if (binding && typeof binding === "object" && (binding as { type?: unknown }).type === "secret_ref") {
    return binding as SecretRef;
  }
  return null;
}

function envBindingKind(binding: EnvBinding): "plain" | "secret_ref" {
  return asSecretRef(binding) ? "secret_ref" : "plain";
}

function formatVersionSelector(version: SecretRef["version"]): string {
  if (version == null || version === "latest") return "latest";
  return `v${version}`;
}

function describeSecretRef(ref: SecretRef): string {
  return `secret ${String(ref.secretId).slice(0, 8)} ${formatVersionSelector(ref.version)}`;
}

function describeEnvBinding(binding: EnvBinding | undefined): string {
  if (binding === undefined) return "—";
  const ref = asSecretRef(binding);
  if (ref) return `secret_ref → ${describeSecretRef(ref)}`;
  return "plain (set)";
}

function summarizeEnv(env: Record<string, EnvBinding> | null): string {
  const entries = Object.entries(normalizeEnv(env));
  if (entries.length === 0) return "";
  const secretCount = entries.filter(([, binding]) => envBindingKind(binding) === "secret_ref").length;
  const keyLabel = entries.length === 1 ? "key" : "keys";
  if (secretCount === 0) return `${entries.length} ${keyLabel}`;
  return `${entries.length} ${keyLabel} (${secretCount} secret ${secretCount === 1 ? "ref" : "refs"})`;
}

type EnvDiffCounts = { added: number; removed: number; changed: number; total: number };

function summarizeEnvDiffCounts(
  current: Record<string, EnvBinding> | null,
  target: Record<string, EnvBinding> | null,
): EnvDiffCounts {
  const currentRec = normalizeEnv(current);
  const targetRec = normalizeEnv(target);
  let added = 0;
  let removed = 0;
  let changed = 0;
  const keys = new Set<string>([...Object.keys(currentRec), ...Object.keys(targetRec)]);
  for (const key of keys) {
    const inCurrent = key in currentRec;
    const inTarget = key in targetRec;
    if (inTarget && !inCurrent) {
      added += 1;
      continue;
    }
    if (!inTarget && inCurrent) {
      removed += 1;
      continue;
    }
    if (JSON.stringify(currentRec[key]) !== JSON.stringify(targetRec[key])) {
      changed += 1;
    }
  }
  return { added, removed, changed, total: added + removed + changed };
}

function formatEnvDiffCounts(counts: EnvDiffCounts): string {
  const parts: string[] = [];
  if (counts.added > 0) parts.push(`${counts.added} ${counts.added === 1 ? "key" : "keys"} added`);
  if (counts.removed > 0) parts.push(`${counts.removed} ${counts.removed === 1 ? "key" : "keys"} removed`);
  if (counts.changed > 0) parts.push(`${counts.changed} ${counts.changed === 1 ? "key" : "keys"} changed`);
  return parts.join(", ");
}

function compareEnv(
  oldEnv: Record<string, EnvBinding> | null,
  newEnv: Record<string, EnvBinding> | null,
  changes: FieldChange[],
) {
  const oldRec = normalizeEnv(oldEnv);
  const newRec = normalizeEnv(newEnv);
  const keys = [...new Set<string>([...Object.keys(oldRec), ...Object.keys(newRec)])].sort();
  for (const key of keys) {
    const oldBinding = oldRec[key];
    const newBinding = newRec[key];
    const inOld = key in oldRec;
    const inNew = key in newRec;
    if (inNew && !inOld) {
      changes.push({ field: `Env added (${key})`, oldValue: "—", newValue: describeEnvBinding(newBinding) });
      continue;
    }
    if (!inNew && inOld) {
      changes.push({ field: `Env removed (${key})`, oldValue: describeEnvBinding(oldBinding), newValue: "—" });
      continue;
    }
    if (JSON.stringify(oldBinding) === JSON.stringify(newBinding)) continue;
    const oldKind = envBindingKind(oldBinding);
    const newKind = envBindingKind(newBinding);
    if (oldKind !== newKind) {
      changes.push({
        field: `Env ${key} binding kind`,
        oldValue: describeEnvBinding(oldBinding),
        newValue: describeEnvBinding(newBinding),
      });
      continue;
    }
    if (newKind === "secret_ref") {
      const oldRef = asSecretRef(oldBinding)!;
      const newRef = asSecretRef(newBinding)!;
      if (oldRef.secretId !== newRef.secretId) {
        changes.push({
          field: `Env ${key} secret`,
          oldValue: describeEnvBinding(oldBinding),
          newValue: describeEnvBinding(newBinding),
        });
        continue;
      }
      changes.push({ field: `Env ${key} version`, oldValue: describeSecretRef(oldRef), newValue: describeSecretRef(newRef) });
      continue;
    }
    changes.push({ field: `Env ${key} value`, oldValue: "plain (set)", newValue: "plain (changed)" });
  }
}

function summarizeVariables(variables: RoutineVariable[]): string {
  if (variables.length === 0) return "(none)";
  return variables.map((variable) => `${variable.name}=${formatVariableDefault(variable)}`).join(", ");
}

function compareTriggers(
  oldTriggers: RoutineRevisionSnapshotTrigger[],
  newTriggers: RoutineRevisionSnapshotTrigger[],
  changes: FieldChange[],
) {
  const byId = new Map<string, { old?: RoutineRevisionSnapshotTrigger; next?: RoutineRevisionSnapshotTrigger }>();
  for (const trigger of oldTriggers) byId.set(trigger.id, { old: trigger });
  for (const trigger of newTriggers) {
    const existing = byId.get(trigger.id) ?? {};
    byId.set(trigger.id, { ...existing, next: trigger });
  }
  for (const [, pair] of byId) {
    if (pair.old && !pair.next) {
      changes.push({
        field: `Trigger removed (${pair.old.label ?? pair.old.kind})`,
        oldValue: summarizeTriggerSnapshot(pair.old),
        newValue: null,
      });
    } else if (!pair.old && pair.next) {
      changes.push({
        field: `Trigger added (${pair.next.label ?? pair.next.kind})`,
        oldValue: null,
        newValue: summarizeTriggerSnapshot(pair.next),
      });
    } else if (pair.old && pair.next) {
      const oldSummary = summarizeTriggerSnapshot(pair.old);
      const newSummary = summarizeTriggerSnapshot(pair.next);
      if (oldSummary !== newSummary || pair.old.enabled !== pair.next.enabled) {
        changes.push({
          field: `Trigger ${pair.next.label ?? pair.next.kind}`,
          oldValue: `${oldSummary} (${pair.old.enabled ? "enabled" : "disabled"})`,
          newValue: `${newSummary} (${pair.next.enabled ? "enabled" : "disabled"})`,
        });
      }
    }
  }
}
