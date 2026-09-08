"use client";

import { useState, useMemo } from "react";
import { MoreHorizontal, Target, Trash2 } from "lucide-react";
import {
  EditorialButton,
  EditorialDialog,
  PanelEmptyState,
  PanelMasthead,
} from "@/components/teams/shell";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader as DialogHeaderUi,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { teamsDetailUrl } from "@/lib/shellNav";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { notifyApiError } from "@/lib/notify";
import { useGoalMutations } from "@/components/teams/goals/hooks/useGoalMutations";
import { CreateGoalDialog } from "@/components/teams/goals/CreateGoalDialog";
import {
  GOAL_STATUSES,
} from "@/components/teams/shared/types";

interface Goal {
  id: string;
  title: string;
  description?: string;
  parent_id?: string | null;
  // Upstream rows are camelCase (parentId); the BFF proxies upstream verbatim.
  parentId?: string | null;
  status?: string;
}

interface GoalNode extends Goal {
  children: GoalNode[];
}

function parentOf(g: Goal): string | null | undefined {
  return g.parentId ?? g.parent_id;
}

function buildTree(goals: Goal[]): GoalNode[] {
  const byId: Record<string, GoalNode> = {};
  goals.forEach((g) => {
    byId[g.id] = { ...g, children: [] };
  });
  const roots: GoalNode[] = [];
  goals.forEach((g) => {
    const node = byId[g.id];
    const parent = parentOf(g);
    if (parent && byId[parent]) {
      byId[parent].children.push(node);
    } else {
      roots.push(node);
    }
  });
  return roots;
}

function goalStatusColor(status?: string): string {
  const s = (status ?? "").toLowerCase();
  if (s === "active" || s === "achieved") return "var(--midnight)";
  return "var(--midnight-muted)";
}

function GoalRow({
  node,
  depth,
  onOpen,
  onEdit,
  onDelete,
}: {
  node: GoalNode;
  depth: number;
  onOpen: (id: string) => void;
  onEdit: (goal: Goal) => void;
  onDelete: (goal: Goal) => void;
}) {
  return (
    <>
      <li
        className="p-3 border-b border-[var(--edge)] last:border-b-0 flex items-start gap-2"
        style={{ paddingLeft: `${depth * 1.5 + 0.75}rem` }}
      >
        <button
          type="button"
          onClick={() => onOpen(node.id)}
          className="flex-1 min-w-0 text-left rounded-sm px-1 -mx-1 transition-colors hover:bg-[var(--cream-deep)]"
        >
          <div className="flex items-center gap-2">
            <span className="font-medium text-[var(--midnight)]">{node.title}</span>
            {node.status && (
              <span
                className="label-caps px-1.5 py-0.5 rounded-sm shrink-0"
                style={{ background: "var(--edge)", color: goalStatusColor(node.status) }}
              >
                {node.status}
              </span>
            )}
          </div>
          {node.description && (
            <div className="text-xs text-[var(--midnight-muted)] mt-0.5">{node.description}</div>
          )}
        </button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-sm transition-colors hover:bg-[var(--cream-deep)]"
              style={{ color: "var(--midnight-muted)" }}
              aria-label="Goal actions"
            >
              <MoreHorizontal size={16} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => onEdit(node)}>Edit goal</DropdownMenuItem>
            <DropdownMenuItem
              variant="destructive"
              onSelect={(e) => {
                e.preventDefault();
                onDelete(node);
              }}
            >
              <Trash2 size={16} />
              Delete goal
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </li>
      {node.children.map((c) => (
        <GoalRow
          key={c.id}
          node={c}
          depth={depth + 1}
          onOpen={onOpen}
          onEdit={onEdit}
          onDelete={onDelete}
        />
      ))}
    </>
  );
}

export function GoalsPanel() {
  const { read } = useTeamsApi();
  const { data, isLoading, error, mutate } = read<{ goals: Goal[] }>("/goals");
  const nav = useInlineTeamsNav();
  const goalMutations = useGoalMutations();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Goal | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Goal | null>(null);
  const [deleting, setDeleting] = useState(false);

  const tree = useMemo(() => buildTree(data?.goals ?? []), [data]);

  // With a nav provider the detail opens through the shell's openDetail;
  // without one, commit the canonical in-shell URL through the History API so
  // the swap stays client-side either way. Mirrors RoutinesPanel.
  const openGoal = (id: string) => {
    if (nav) {
      nav.openDetail("goal-detail", id);
    } else if (typeof window !== "undefined") {
      window.history.pushState(null, "", teamsDetailUrl("goal", id));
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await goalMutations.remove(deleteTarget.id);
      setDeleteTarget(null);
      mutate();
    } catch (e) {
      // Surface the failure rather than silently leaving the row in place —
      // the delete didn't happen and the user needs to know.
      notifyApiError(e, "Couldn't delete the goal. Try again.");
    } finally {
      setDeleting(false);
    }
  };

  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--err)]">
        Error: {String(error)}
      </div>
    );

  return (
    <div className="editorial-page flex flex-col gap-4 p-4 sm:p-5 lg:p-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Strategy"
        title="Goals"
        actions={
          <EditorialButton variant="primary" onClick={() => setCreating(true)}>
            New goal
          </EditorialButton>
        }
      />

      {isLoading ? (
        <div className="font-editorial-serif text-xl italic text-[var(--midnight-muted)]">
          Loading…
        </div>
      ) : tree.length === 0 ? (
        <PanelEmptyState
          icon={Target}
          headline="No goals yet."
          supporting="Define a goal to give your agents direction — break it into nested sub-goals as needed."
          action={{ label: "New goal", onClick: () => setCreating(true) }}
        />
      ) : (
        <ul className="editorial-card overflow-hidden">
          {tree.map((root) => (
            <GoalRow
              key={root.id}
              node={root}
              depth={0}
              onOpen={openGoal}
              onEdit={setEditing}
              onDelete={setDeleteTarget}
            />
          ))}
        </ul>
      )}

      {creating && (
        <CreateGoalDialog
          goals={data?.goals ?? []}
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            mutate();
          }}
        />
      )}

      {editing && (
        <EditGoalDialog
          goal={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            mutate();
          }}
        />
      )}

      <Dialog
        open={!!deleteTarget}
        onOpenChange={(o) => !o && setDeleteTarget(null)}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeaderUi>
            <DialogTitle>Delete goal?</DialogTitle>
            <DialogDescription>
              This permanently deletes {deleteTarget?.title}. This action cannot
              be undone.
            </DialogDescription>
          </DialogHeaderUi>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" disabled={deleting}>
                Cancel
              </Button>
            </DialogClose>
            <Button
              type="button"
              variant="destructive"
              disabled={deleting}
              onClick={() => void confirmDelete()}
            >
              {deleting ? "Deleting…" : "Delete goal"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function EditGoalDialog({
  goal,
  onClose,
  onSaved,
}: {
  goal: Goal;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { patch } = useTeamsApi();
  const [title, setTitle] = useState(goal.title);
  const [description, setDescription] = useState(goal.description ?? "");
  const [status, setStatus] = useState(goal.status ?? "planned");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setSubmitting(true);
    setErr(null);
    try {
      await patch(`/goals/${goal.id}`, { title, description, status });
      onSaved();
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  }

  return (
    <EditorialDialog open onOpenChange={(o) => !o && onClose()} title="Edit goal">
      <div className="flex flex-col gap-3">
        <label className="block">
          <span className="text-sm text-[var(--midnight-soft)]">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
          />
        </label>
        <label className="block">
          <span className="text-sm text-[var(--midnight-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
            rows={3}
          />
        </label>
        <label className="block">
          <span className="text-sm text-[var(--midnight-soft)]">Status</span>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
          >
            {GOAL_STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
        {err && (
          <div
            className="text-[var(--err)] text-sm rounded-sm px-3 py-2"
            style={{
              border:
                "1px solid var(--err)",
              background:
                "var(--cream)",
            }}
          >
            {err}
          </div>
        )}
        <div className="flex justify-end gap-2 mt-2">
          <EditorialButton variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </EditorialButton>
          <EditorialButton size="sm" onClick={submit} disabled={submitting || !title}>
            {submitting ? "Saving…" : "Save"}
          </EditorialButton>
        </div>
      </div>
    </EditorialDialog>
  );
}
