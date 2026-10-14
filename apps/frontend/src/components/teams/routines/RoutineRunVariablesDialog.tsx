// apps/frontend/src/components/teams/routines/RoutineRunVariablesDialog.tsx

// Ported from upstream Paperclip's `RoutineRunVariablesDialog`
// (paperclip/ui/src/components/RoutineRunVariablesDialog.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Copy, the agent (required) + project pickers,
// the per-type variable inputs, required-value gating, footer notices and the
// submit coercion (number -> Number, boolean -> true/false, else String;
// blank values omitted) are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - execution-workspace selection (`IssueWorkspaceCard`, the instance
//     `enableIsolatedWorkspaces` experimental flag, the auto-filled
//     workspace-branch variable) is not ported: Hermes runs have no Paperclip
//     execution workspaces, so `routineRunNeedsConfiguration` is variables-only
//   - InlineEntitySelector -> our AssigneePicker / ProjectPicker (which carry
//     their own recent-pick ordering)
//   - shadcn Dialog/Button/Input/Select -> the editorial shell; amber notices
//     -> the `--warn` brand token

"use client";

import { useMemo, useState } from "react";
import { AssigneePicker } from "@/components/teams/issues/pickers/AssigneePicker";
import { ProjectPicker } from "@/components/teams/issues/pickers/ProjectPicker";
import { EditorialButton } from "@/components/teams/shell/EditorialButton";
import { EditorialDialog } from "@/components/teams/shell/EditorialDialog";
import { EditorialSelect } from "@/components/teams/shell/EditorialSelect";
import type {
  CompanyAgent,
  IssueProject,
  RoutineVariable,
  RoutineVariableValue,
} from "@/components/teams/shared/types";

const FIELD_CLASS =
  "w-full rounded-sm border border-[var(--edge)] bg-[var(--cream)] px-3 py-2 text-ui text-[var(--midnight)]";

function buildInitialValues(variables: RoutineVariable[]) {
  return Object.fromEntries(variables.map((variable) => [variable.name, variable.defaultValue ?? ""]));
}

function isMissingRequiredValue(value: unknown) {
  return value == null || (typeof value === "string" && value.trim().length === 0);
}

export function routineRunNeedsConfiguration(input: { variables: RoutineVariable[] }) {
  return input.variables.length > 0;
}

export interface RoutineRunDialogSubmitData {
  variables?: Record<string, RoutineVariableValue>;
  assigneeAgentId?: string | null;
  projectId?: string | null;
}

export function RoutineRunVariablesDialog({
  open,
  onOpenChange,
  routineName,
  projects,
  agents,
  defaultProjectId,
  defaultAssigneeAgentId,
  variables,
  isPending,
  error,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  routineName?: string | null;
  projects: IssueProject[];
  agents: CompanyAgent[];
  defaultProjectId?: string | null;
  defaultAssigneeAgentId?: string | null;
  variables: RoutineVariable[];
  isPending: boolean;
  /** Inline failure text (ours: the dialog stays open on a failed run). */
  error?: string | null;
  onSubmit: (data: RoutineRunDialogSubmitData) => void;
}) {
  const initialSelection = () => ({
    assigneeAgentId: defaultAssigneeAgentId ?? "",
    projectId: defaultProjectId ?? "",
  });
  const [values, setValues] = useState<Record<string, unknown>>(() => buildInitialValues(variables));
  const [selection, setSelection] = useState(initialSelection);

  // Upstream resets to the routine defaults in an effect each time the dialog
  // opens; ours does the same reset during render on the open edge (React's
  // "adjust state on prop change" pattern), which the lint rule requires.
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setValues(buildInitialValues(variables));
      setSelection(initialSelection());
    }
  }

  const assignableAgents = useMemo(
    () => agents.filter((agent) => (agent as { status?: string }).status !== "terminated"),
    [agents],
  );

  const missingRequired = useMemo(
    () =>
      variables
        .filter((variable) => variable.required)
        .filter((variable) => isMissingRequiredValue(values[variable.name]))
        .map((variable) => variable.label || variable.name),
    [values, variables],
  );

  const canSubmit = selection.assigneeAgentId.trim().length > 0 && missingRequired.length === 0;

  const setValue = (name: string, next: unknown) =>
    setValues((current) => ({ ...current, [name]: next }));

  const submit = () => {
    const nextVariables: Record<string, RoutineVariableValue> = {};
    for (const variable of variables) {
      const rawValue = values[variable.name];
      if (isMissingRequiredValue(rawValue)) continue;
      if (variable.type === "number") {
        nextVariables[variable.name] = Number(rawValue);
      } else if (variable.type === "boolean") {
        nextVariables[variable.name] = rawValue === true;
      } else {
        nextVariables[variable.name] = String(rawValue);
      }
    }
    onSubmit({
      variables: nextVariables,
      assigneeAgentId: selection.assigneeAgentId,
      projectId: selection.projectId || null,
    });
  };

  return (
    <EditorialDialog
      open={open}
      onOpenChange={(next) => !isPending && onOpenChange(next)}
      title="Run routine"
      description={
        <>
          {routineName && <span className="block">{routineName}</span>}
          Choose the agent and optional project for this one run. Routine defaults are prefilled
          and won&apos;t be changed.
        </>
      }
      footer={
        <>
          {!selection.assigneeAgentId ? (
            <p className="mr-auto text-caption" style={{ color: "var(--warn)" }}>
              Default agent required for this run.
            </p>
          ) : missingRequired.length > 0 ? (
            <p className="mr-auto text-caption" style={{ color: "var(--warn)" }}>
              Missing: {missingRequired.join(", ")}
            </p>
          ) : (
            <span className="mr-auto" />
          )}
          <EditorialButton
            variant="ghost"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={isPending}
          >
            Cancel
          </EditorialButton>
          <EditorialButton
            variant="primary"
            size="sm"
            onClick={submit}
            disabled={isPending || !canSubmit}
          >
            {isPending ? "Running..." : "Run routine"}
          </EditorialButton>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {error && (
          <p role="alert" className="text-ui" style={{ color: "var(--err)" }}>
            {error}
          </p>
        )}
        <div className="grid gap-4 md:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <span className="text-caption text-[var(--midnight-soft)]">Agent *</span>
            <AssigneePicker
              agents={assignableAgents}
              selectedAgentId={selection.assigneeAgentId || null}
              onChange={(assigneeAgentId) =>
                setSelection((current) => ({ ...current, assigneeAgentId: assigneeAgentId ?? "" }))
              }
              variant="inline"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <span className="text-caption text-[var(--midnight-soft)]">Project</span>
            <ProjectPicker
              projects={projects}
              selectedProjectId={selection.projectId || null}
              onChange={(projectId) =>
                setSelection((current) => ({ ...current, projectId: projectId ?? "" }))
              }
            />
          </div>
        </div>

        {variables.map((variable) => {
          const id = `routine-run-var-${variable.name}`;
          const label = `${variable.label || variable.name}${variable.required ? " *" : ""}`;
          const raw = values[variable.name];
          return (
            <div key={variable.name} className="flex flex-col gap-1.5">
              <label htmlFor={id} className="text-caption text-[var(--midnight-soft)]">
                {label}
              </label>
              {variable.type === "textarea" ? (
                <textarea
                  id={id}
                  rows={4}
                  className={FIELD_CLASS}
                  value={typeof raw === "string" ? raw : ""}
                  onChange={(event) => setValue(variable.name, event.target.value)}
                />
              ) : variable.type === "boolean" ? (
                <EditorialSelect
                  options={[
                    { value: "__unset__", label: "No value" },
                    { value: "true", label: "True" },
                    { value: "false", label: "False" },
                  ]}
                  value={raw === true ? "true" : raw === false ? "false" : "__unset__"}
                  onValueChange={(next) =>
                    setValue(variable.name, next === "__unset__" ? "" : next === "true")
                  }
                />
              ) : variable.type === "select" ? (
                <EditorialSelect
                  options={[
                    { value: "__unset__", label: "No value" },
                    ...(variable.options ?? []).map((option) => ({ value: option, label: option })),
                  ]}
                  value={typeof raw === "string" && raw ? raw : "__unset__"}
                  onValueChange={(next) => setValue(variable.name, next === "__unset__" ? "" : next)}
                  placeholder="Choose a value"
                />
              ) : (
                <input
                  id={id}
                  type={
                    variable.type === "date" ? "date" : variable.type === "number" ? "number" : "text"
                  }
                  className={FIELD_CLASS}
                  value={raw == null ? "" : String(raw)}
                  onChange={(event) => setValue(variable.name, event.target.value)}
                />
              )}
            </div>
          );
        })}
      </div>
    </EditorialDialog>
  );
}
