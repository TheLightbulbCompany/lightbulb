"use client";

// The "New goal" dialog, lifted out of GoalsPanel so it survives that panel
// being taken off the rail. Goals now live as a grouping on Tasks; without
// this extraction, absorbing the tab would have quietly removed the only way
// an owner could CREATE a goal — the tab's other jobs (detail, edit, delete)
// already survive through the goal-detail page a group heading links to.

import { useState } from "react";
import { EditorialButton, EditorialDialog } from "@/components/teams/shell";
import { GoalPicker } from "@/components/teams/goals/GoalPicker";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import {
  GOAL_LEVELS,
  GOAL_STATUSES,
  type GoalLevel,
  type GoalStatus,
} from "@/components/teams/shared/types";
import type { GoalPickerOption } from "@/components/teams/goals/GoalPicker";

export function CreateGoalDialog({
  goals,
  onClose,
  onCreated,
}: {
  /** Only ever forwarded to the parent-goal picker, so it is typed as what
   *  that picker consumes rather than the full Goal — GoalsPanel and Tasks
   *  hold different (both narrower) goal shapes and neither needs widening. */
  goals: GoalPickerOption[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [parentId, setParentId] = useState<string | null>(null);
  // Defaults mirror upstream createGoalSchema (level="task", status="planned")
  // so the server default and the form's initial state agree.
  const [level, setLevel] = useState<GoalLevel>("task");
  const [status, setStatus] = useState<GoalStatus>("planned");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setSubmitting(true);
    setErr(null);
    try {
      // CreateGoalBody fields: title, description?, parent_id?, level?, status?
      const body: Record<string, unknown> = { title, level, status };
      if (description) body.description = description;
      if (parentId) body.parent_id = parentId;
      await post("/goals", body);
      onCreated();
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  }

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="New goal"
    >
      <div className="flex flex-col gap-3">
        <label className="block">
          <span className="text-ui text-[var(--midnight-soft)]">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
          />
        </label>
        <label className="block">
          <span className="text-ui text-[var(--midnight-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
            rows={3}
          />
        </label>
        {/* Parent goal picker replaces the old raw "Parent goal ID" text input
            — resolves from the existing /goals list (mirrors the issues
            AssigneePicker/ProjectPicker idiom). */}
        <div className="block">
          <span className="text-ui text-[var(--midnight-soft)]">Parent goal</span>
          <div className="mt-1">
            <GoalPicker
              goals={goals}
              selectedGoalId={parentId}
              onChange={setParentId}
              className="border border-[var(--edge)] rounded-sm px-3 py-2 bg-[var(--cream)]"
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <span className="text-ui text-[var(--midnight-soft)]">Level</span>
            <select
              value={level}
              onChange={(e) => setLevel(e.target.value as GoalLevel)}
              className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)] capitalize"
            >
              {GOAL_LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="text-ui text-[var(--midnight-soft)]">Status</span>
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value as GoalStatus)}
              className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)] capitalize"
            >
              {GOAL_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        </div>
        {err && (
          <div
            className="text-[var(--err)] text-ui rounded-sm px-3 py-2"
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
          <EditorialButton
            size="sm"
            onClick={submit}
            disabled={submitting || !title}
          >
            {submitting ? "Creating…" : "Create"}
          </EditorialButton>
        </div>
      </div>
    </EditorialDialog>
  );
}
