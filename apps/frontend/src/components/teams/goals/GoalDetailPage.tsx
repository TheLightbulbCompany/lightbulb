// apps/frontend/src/components/teams/goals/GoalDetailPage.tsx

// Ported from upstream Paperclip's pages/GoalDetail.tsx top-level layout
// (paperclip/ui/src/pages/GoalDetail.tsx) (MIT, © 2025 Paperclip AI).
// Mirrors the issue IssueDetailPage.tsx structure exactly: a [1fr 320px] grid
// with a main column (breadcrumb, header, description, sub-goals + linked
// projects sections) and a sticky non-scrollable Properties aside.
// Drops asset uploads, breadcrumb context, and the panel-toggle button.

"use client";

import { useState } from "react";
import Link from "next/link";
import { ChevronRight, MoreHorizontal, Target, Trash2 } from "lucide-react";
import { GoalHeader } from "./GoalHeader";
import { GoalProperties } from "./GoalProperties";
import { GoalStatusIcon } from "./GoalStatusIcon";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { useGoalDetail } from "./hooks/useGoalDetail";
import { useGoalMutations } from "./hooks/useGoalMutations";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { notifyApiError } from "@/lib/notify";
import {
  handleShellLinkClick,
  replaceSearchParams,
  teamsDetailUrl,
  teamsPanelUrl,
} from "@/lib/shellNav";
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
import type { CompanyAgent, Goal } from "@/components/teams/shared/types";

export interface GoalDetailPageProps {
  goalId: string;
}

// The /teams/projects envelope proxies the raw upstream project rows, which
// carry the goal-linkage fields (goalIds / goals / goalId) even though the slim
// shared IssueProject type omits them. Capture just what the linked-projects
// section needs here rather than bloating the shared type.
interface GoalLinkedProject {
  id: string;
  name: string;
  status?: string | null;
  goalId?: string | null;
  goalIds?: string[] | null;
  goals?: { id: string; title?: string | null }[] | null;
}

function projectLinksGoal(p: GoalLinkedProject, goalId: string): boolean {
  if (p.goalIds?.includes(goalId)) return true;
  if (p.goals?.some((g) => g.id === goalId)) return true;
  return p.goalId === goalId;
}

export function GoalDetailPage({ goalId }: GoalDetailPageProps) {
  const {
    goal,
    allGoals,
    projects,
    issues,
    issuesLoading,
    isLoading,
    isError,
    error,
  } = useGoalDetail(goalId);
  const mutations = useGoalMutations();
  const { read } = useTeamsApi();
  const { data: agentsResp } = read<{ agents: CompanyAgent[] }>("/agents");
  const agents = agentsResp?.agents ?? [];
  const nav = useInlineTeamsNav();

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const confirmDelete = async () => {
    setDeleting(true);
    try {
      await mutations.remove(goalId);
      setDeleteOpen(false);
      // The goal is gone — swap the now-dead detail view for the Goals list.
      // In-shell this goes through the nav context (a replace-only URL move so
      // Back never lands on the deleted goal); standalone / test renders (no
      // provider) fall back to the direct window.location rewrite.
      if (nav) {
        // Pass our own id so the desktop dock closes THIS detail panel, not
        // whichever panel happens to be active when the delete confirms.
        nav.leaveToList("teams-goals", goalId);
      } else {
        const params = new URLSearchParams(window.location.search);
        params.set("panel", "teams-goals");
        params.delete("detailId");
        replaceSearchParams(params);
      }
    } catch (e) {
      // Surface the failure instead of silently leaving the dialog open with
      // no feedback — the goal is still there; the user needs to know.
      notifyApiError(e, "Couldn't delete the goal. Try again.");
    } finally {
      setDeleting(false);
    }
  };

  if (isLoading && !goal) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-serif), serif",
          color: "var(--midnight-muted)",
        }}
      >
        Loading…
      </div>
    );
  }

  if (isError && !goal) {
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center p-8 text-sm"
        style={{ color: "var(--err)" }}
      >
        <p>Failed to load goal.</p>
        {error?.message && (
          <p className="mt-1" style={{ color: "var(--midnight-muted)" }}>
            {error.message}
          </p>
        )}
      </div>
    );
  }

  if (!goal) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-serif), serif",
          color: "var(--midnight-muted)",
        }}
      >
        Goal not found.
      </div>
    );
  }

  const childGoals = allGoals.filter((g: Goal) => g.parentId === goalId);
  const linkedProjects = (projects as unknown as GoalLinkedProject[]).filter((p) =>
    projectLinksGoal(p, goalId),
  );
  const linkedTasks = issues.filter((i) => i.goalId === goalId);

  return (
    <div
      className="grid grid-cols-1 @3xl:grid-cols-[minmax(0,1fr)_320px] gap-6 p-4 sm:p-6"
      style={{ background: "var(--cream)", color: "var(--midnight)" }}
    >
      <main className="flex flex-col gap-6 min-w-0">
        <nav
          className="flex items-center gap-1 text-xs"
          style={{ color: "var(--midnight-muted)" }}
          aria-label="Breadcrumb"
        >
          <Link
            href={teamsPanelUrl("goals")}
            prefetch={false}
            onClick={(e) => handleShellLinkClick(e, teamsPanelUrl("goals"))}
            className="hover:underline"
          >
            Goals
          </Link>
          <ChevronRight size={12} strokeWidth={1.75} />
          <span className="truncate" style={{ color: "var(--midnight-soft)" }}>
            {goal.title}
          </span>
        </nav>

        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <GoalHeader
              goal={goal}
              onTitleSave={async (title) => {
                await mutations.update(goalId, { title });
              }}
              onStatusChange={async (status) => {
                await mutations.update(goalId, { status });
              }}
            />
          </div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className="mt-1 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-sm transition-colors hover:bg-[var(--cream-deep)]"
                style={{ color: "var(--midnight-muted)" }}
                aria-label="Goal actions"
              >
                <MoreHorizontal size={16} />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                variant="destructive"
                onSelect={(e) => {
                  e.preventDefault();
                  setDeleteOpen(true);
                }}
              >
                <Trash2 size={16} />
                Delete goal
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        {goal.description && (
          <div
            className="text-sm whitespace-pre-wrap pl-3"
            style={{
              borderLeft: "2px solid var(--edge)",
              color: "var(--midnight-soft)",
            }}
          >
            {goal.description}
          </div>
        )}

        <section className="flex flex-col gap-3">
          <h2 className="section-label">
            <b>Sub-goals ({childGoals.length})</b>
          </h2>
          {childGoals.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--midnight-muted)" }}>
              No sub-goals.
            </p>
          ) : (
            <ul
              className="flex flex-col rounded-sm overflow-hidden"
              style={{ border: "1px solid var(--edge)" }}
            >
              {childGoals.map((child) => (
                <li
                  key={child.id}
                  style={{ borderBottom: "1px solid var(--edge)" }}
                  className="last:border-b-0"
                >
                  <Link
                    href={teamsDetailUrl("goal", child.id)}
                    prefetch={false}
                    onClick={(e) =>
                      handleShellLinkClick(e, teamsDetailUrl("goal", child.id))
                    }
                    className="flex items-center gap-2 p-3 transition-colors hover:bg-[var(--cream-deep)]"
                  >
                    <GoalStatusIcon status={child.status} />
                    <span
                      className="min-w-0 flex-1 truncate text-sm"
                      style={{ color: "var(--midnight)" }}
                    >
                      {child.title}
                    </span>
                    <span
                      className="label-caps shrink-0"
                      style={{ color: "var(--midnight-muted)" }}
                    >
                      {child.level}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="section-label">
            <b>Tasks ({linkedTasks.length})</b>
          </h2>
          {linkedTasks.length === 0 ? (
            // The issues fetch is company-wide and lands well after the
            // single-row goal fetch, and an unresolved fetch normalizes to []
            // — so without this the section asserts "No tasks" for a goal that
            // has plenty, until the list arrives.
            <p className="text-sm italic" style={{ color: "var(--midnight-muted)" }}>
              {issuesLoading
                ? "Loading…"
                : "No tasks. Link one from its Goal row on the task page."}
            </p>
          ) : (
            <ul
              className="flex flex-col rounded-sm overflow-hidden"
              style={{ border: "1px solid var(--edge)" }}
            >
              {linkedTasks.map((task) => (
                <li
                  key={task.id}
                  style={{ borderBottom: "1px solid var(--edge)" }}
                  className="last:border-b-0"
                >
                  <Link
                    href={teamsDetailUrl("issue", task.id)}
                    prefetch={false}
                    onClick={(e) =>
                      handleShellLinkClick(e, teamsDetailUrl("issue", task.id))
                    }
                    className="flex items-center gap-2 p-3 transition-colors hover:bg-[var(--cream-deep)]"
                  >
                    <StatusIcon status={task.status} />
                    {task.identifier && (
                      <span
                        className="shrink-0 text-xs tabular-nums"
                        style={{ color: "var(--midnight-muted)" }}
                      >
                        {task.identifier}
                      </span>
                    )}
                    <span
                      className="min-w-0 flex-1 truncate text-sm"
                      style={{ color: "var(--midnight)" }}
                    >
                      {task.title}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="section-label">
            <b>Linked projects ({linkedProjects.length})</b>
          </h2>
          {linkedProjects.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--midnight-muted)" }}>
              No linked projects.
            </p>
          ) : (
            <ul
              className="flex flex-col rounded-sm overflow-hidden"
              style={{ border: "1px solid var(--edge)" }}
            >
              {linkedProjects.map((project) => (
                <li
                  key={project.id}
                  style={{ borderBottom: "1px solid var(--edge)" }}
                  className="last:border-b-0"
                >
                  <Link
                    href={teamsDetailUrl("project", project.id)}
                    prefetch={false}
                    onClick={(e) =>
                      handleShellLinkClick(e, teamsDetailUrl("project", project.id))
                    }
                    className="flex items-center gap-2 p-3 transition-colors hover:bg-[var(--cream-deep)]"
                  >
                    <Target size={16}
                      className="shrink-0"
                      style={{ color: "var(--midnight-muted)" }}
                    />
                    <span
                      className="min-w-0 flex-1 truncate text-sm"
                      style={{ color: "var(--midnight)" }}
                    >
                      {project.name}
                    </span>
                    {project.status && (
                      <span
                        className="label-caps shrink-0"
                        style={{ color: "var(--midnight-muted)" }}
                      >
                        {project.status}
                      </span>
                    )}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>

      <aside className="@3xl:sticky @3xl:top-4 @3xl:self-start">
        <GoalProperties
          goal={goal}
          agents={agents}
          allGoals={allGoals}
          onStatusChange={(status) => {
            void mutations.update(goalId, { status });
          }}
          onLevelChange={(level) => {
            void mutations.update(goalId, { level });
          }}
        />
      </aside>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeaderUi>
            <DialogTitle>Delete goal?</DialogTitle>
            <DialogDescription>
              This permanently deletes {goal.title}. This action cannot be
              undone.
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
