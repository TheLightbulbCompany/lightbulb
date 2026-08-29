// apps/frontend/src/components/teams/inbox/IssueFiltersPopover.tsx

// Ported from upstream Paperclip's IssueFiltersPopover.tsx
// (paperclip/ui/src/components/IssueFiltersPopover.tsx) (MIT, (c) 2025 Paperclip AI).
// The active filter chip + count badge use --midnight / --cream so the
// popover matches the rest of the editorial Teams surface.

import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Bot, Filter, HardDrive, Search, User, X } from "lucide-react";
import { PriorityIcon } from "@/components/teams/shared/components/PriorityIcon";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import {
  defaultIssueFilterState,
  issueFilterArraysEqual,
  issueFilterLabel,
  issuePriorityOrder,
  issueQuickFilterPresets,
  issueStatusOrder,
  toggleIssueFilterValue,
  type IssueFilterState,
} from "@/components/teams/shared/lib/issueFilters";
import { formatAssigneeUserLabel } from "@/components/teams/shared/lib/assignees";

type AgentOption = {
  id: string;
  name: string;
};

type ProjectOption = {
  id: string;
  name: string;
};

type LabelOption = {
  id: string;
  name: string;
  color: string;
};

type WorkspaceOption = {
  id: string;
  name: string;
};

type CreatorOption = {
  id: string;
  label: string;
  kind: "agent" | "user";
  searchText?: string;
};

export function IssueFiltersPopover({
  state,
  onChange,
  activeFilterCount,
  agents,
  projects,
  labels,
  currentUserId,
  enableRoutineVisibilityFilter = false,
  buttonVariant = "ghost",
  iconOnly = false,
  workspaces,
  creators,
}: {
  state: IssueFilterState;
  onChange: (patch: Partial<IssueFilterState>) => void;
  activeFilterCount: number;
  agents?: AgentOption[];
  projects?: ProjectOption[];
  labels?: LabelOption[];
  currentUserId?: string | null;
  enableRoutineVisibilityFilter?: boolean;
  buttonVariant?: "ghost" | "outline";
  iconOnly?: boolean;
  workspaces?: WorkspaceOption[];
  creators?: CreatorOption[];
}) {
  const [creatorSearch, setCreatorSearch] = useState("");
  const creatorOptions = creators ?? [];
  const creatorOptionById = useMemo(
    () => new Map(creatorOptions.map((option) => [option.id, option])),
    [creatorOptions],
  );
  const normalizedCreatorSearch = creatorSearch.trim().toLowerCase();
  const visibleCreatorOptions = useMemo(() => {
    if (!normalizedCreatorSearch) return creatorOptions;
    return creatorOptions.filter((option) =>
      `${option.label} ${option.searchText ?? ""}`.toLowerCase().includes(normalizedCreatorSearch),
    );
  }, [creatorOptions, normalizedCreatorSearch]);
  const selectedCreatorOptions = useMemo(
    () =>
      state.creators.map((creatorId) => {
        const knownOption = creatorOptionById.get(creatorId);
        if (knownOption) return knownOption;
        if (creatorId.startsWith("agent:")) {
          const agentId = creatorId.slice("agent:".length);
          return { id: creatorId, label: agentId.slice(0, 8), kind: "agent" as const };
        }
        const userId = creatorId.startsWith("user:") ? creatorId.slice("user:".length) : creatorId;
        return {
          id: creatorId,
          label: formatAssigneeUserLabel(userId, currentUserId) ?? "User",
          kind: "user" as const,
        };
      }),
    [creatorOptionById, currentUserId, state.creators],
  );

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant={buttonVariant}
          size={iconOnly ? "icon" : "sm"}
          className={`text-xs ${iconOnly ? "relative h-8 w-8 shrink-0" : ""}`}
          style={
            activeFilterCount > 0 ? { color: "var(--midnight)" } : undefined
          }
          title={iconOnly ? (activeFilterCount > 0 ? `Filters: ${activeFilterCount}` : "Filter") : undefined}
        >
          <Filter size={16} className={iconOnly ? "" : "sm:h-3 sm:w-3 sm:mr-1"} />
          {!iconOnly && (
            <span className="hidden sm:inline">
              {activeFilterCount > 0 ? `Filters: ${activeFilterCount}` : "Filter"}
            </span>
          )}
          {!iconOnly && activeFilterCount > 0 ? (
            <span className="ml-0.5 text-2xs font-medium sm:hidden">{activeFilterCount}</span>
          ) : null}
          {iconOnly && activeFilterCount > 0 ? (
            <span
              className="absolute -right-1 -top-1 flex h-3.5 w-3.5 items-center justify-center rounded-full text-2xs font-bold"
              style={{ background: "var(--midnight)", color: "var(--cream)" }}
            >
              {activeFilterCount}
            </span>
          ) : null}
          {!iconOnly && activeFilterCount > 0 ? (
            <X size={12} strokeWidth={1.75}
              className="ml-1 hidden sm:block"
              onClick={(event) => {
                event.stopPropagation();
                onChange(defaultIssueFilterState);
              }}
            />
          ) : null}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-[min(780px,calc(100vw-2rem))] max-h-[min(80vh,42rem)] overflow-y-auto overscroll-contain p-0"
      >
        <div className="space-y-3 p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">Filters</span>
            {activeFilterCount > 0 ? (
              <button
                type="button"
                className="text-xs text-[var(--midnight-muted)] hover:text-[var(--midnight)]"
                onClick={() => onChange(defaultIssueFilterState)}
              >
                Clear
              </button>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <span className="text-xs text-[var(--midnight-muted)]">Quick filters</span>
            <div className="flex flex-wrap gap-1.5">
              {issueQuickFilterPresets.map((preset) => {
                const isActive = issueFilterArraysEqual(state.statuses, preset.statuses);
                return (
                  <button
                    key={preset.label}
                    type="button"
                    className={`rounded-full border px-2.5 py-1 text-xs transition-colors ${
                      isActive
                        ? "border-[var(--midnight)] bg-[var(--midnight)] text-[var(--cream)]"
                        : "border-[var(--edge)] text-[var(--midnight-muted)] hover:border-[var(--midnight-muted)] hover:text-[var(--midnight)]"
                    }`}
                    onClick={() => onChange({ statuses: isActive ? [] : [...preset.statuses] })}
                  >
                    {preset.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="border-t border-[var(--edge)]" />

          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <div className="min-w-0 space-y-3">
              <div className="space-y-1">
                <span className="text-xs text-[var(--midnight-muted)]">Status</span>
                <div className="space-y-0.5">
                  {issueStatusOrder.map((status) => (
                    <label
                      key={status}
                      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 hover:bg-[var(--cream-deep)]"
                    >
                      <Checkbox
                        checked={state.statuses.includes(status)}
                        onCheckedChange={() =>
                          onChange({ statuses: toggleIssueFilterValue(state.statuses, status) })
                        }
                      />
                      <StatusIcon status={status} />
                      <span className="text-sm">{issueFilterLabel(status)}</span>
                    </label>
                  ))}
                </div>
              </div>

              <div className="space-y-1">
                <span className="text-xs text-[var(--midnight-muted)]">Priority</span>
                <div className="space-y-0.5">
                  {issuePriorityOrder.map((priority) => (
                    <label
                      key={priority}
                      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 hover:bg-[var(--cream-deep)]"
                    >
                      <Checkbox
                        checked={state.priorities.includes(priority)}
                        onCheckedChange={() =>
                          onChange({ priorities: toggleIssueFilterValue(state.priorities, priority) })
                        }
                      />
                      <PriorityIcon priority={priority} />
                      <span className="text-sm">{issueFilterLabel(priority)}</span>
                    </label>
                  ))}
                </div>
              </div>
            </div>

            <div className="min-w-0 space-y-3">
              <div className="space-y-1">
                <span className="text-xs text-[var(--midnight-muted)]">Assignee</span>
                <div className="max-h-32 space-y-0.5 overflow-y-auto">
                  <label className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 hover:bg-[var(--cream-deep)]">
                    <Checkbox
                      checked={state.assignees.includes("__unassigned")}
                      onCheckedChange={() =>
                        onChange({
                          assignees: toggleIssueFilterValue(state.assignees, "__unassigned"),
                        })
                      }
                    />
                    <span className="text-sm">No assignee</span>
                  </label>
                  {currentUserId ? (
                    <label className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 hover:bg-[var(--cream-deep)]">
                      <Checkbox
                        checked={state.assignees.includes("__me")}
                        onCheckedChange={() =>
                          onChange({ assignees: toggleIssueFilterValue(state.assignees, "__me") })
                        }
                      />
                      <User size={16} className="text-[var(--midnight-muted)]" />
                      <span className="text-sm">Me</span>
                    </label>
                  ) : null}
                  {(agents ?? []).map((agent) => (
                    <label
                      key={agent.id}
                      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 hover:bg-[var(--cream-deep)]"
                    >
                      <Checkbox
                        checked={state.assignees.includes(agent.id)}
                        onCheckedChange={() =>
                          onChange({
                            assignees: toggleIssueFilterValue(state.assignees, agent.id),
                          })
                        }
                      />
                      <span className="text-sm">{agent.name}</span>
                    </label>
                  ))}
                </div>
              </div>

              {creatorOptions.length > 0 ? (
                <div className="space-y-1">
                  <span className="text-xs text-[var(--midnight-muted)]">Creator</span>
                  {selectedCreatorOptions.length > 0 ? (
                    <div className="flex flex-wrap gap-1">
                      {selectedCreatorOptions.map((creator) => (
                        <Badge key={creator.id} variant="secondary" className="gap-1 pr-1">
                          {creator.kind === "agent" ? (
                            <Bot size={12} strokeWidth={1.75} />
                          ) : (
                            <User size={12} strokeWidth={1.75} />
                          )}
                          <span>{creator.label}</span>
                          <button
                            type="button"
                            className="rounded-full p-0.5 hover:bg-[var(--cream-deep)]"
                            onClick={() =>
                              onChange({
                                creators: state.creators.filter((value) => value !== creator.id),
                              })
                            }
                            aria-label={`Remove creator ${creator.label}`}
                          >
                            <X size={12} strokeWidth={1.75} />
                          </button>
                        </Badge>
                      ))}
                    </div>
                  ) : null}
                  <div className="relative">
                    <Search size={12} strokeWidth={1.75} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-[var(--midnight-muted)]" />
                    <Input
                      value={creatorSearch}
                      onChange={(event) => setCreatorSearch(event.target.value)}
                      placeholder="Search creators..."
                      className="h-8 pl-7 text-xs"
                    />
                  </div>
                  <div className="max-h-32 space-y-0.5 overflow-y-auto">
                    {visibleCreatorOptions.length > 0 ? (
                      visibleCreatorOptions.map((creator) => {
                        const selected = state.creators.includes(creator.id);
                        return (
                          <button
                            key={creator.id}
                            type="button"
                            className={`flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-sm ${
                              selected
                                ? "bg-[var(--cream-deeper)] text-[var(--midnight)]"
                                : "text-[var(--midnight-muted)] hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
                            }`}
                            onClick={() =>
                              onChange({
                                creators: toggleIssueFilterValue(state.creators, creator.id),
                              })
                            }
                          >
                            {creator.kind === "agent" ? (
                              <Bot size={16} />
                            ) : (
                              <User size={16} />
                            )}
                            <span className="min-w-0 flex-1 truncate">{creator.label}</span>
                            {selected ? <X size={12} strokeWidth={1.75} /> : null}
                          </button>
                        );
                      })
                    ) : (
                      <div className="px-2 py-1 text-xs text-[var(--midnight-muted)]">
                        No creators match.
                      </div>
                    )}
                  </div>
                </div>
              ) : null}

              {projects && projects.length > 0 ? (
                <div className="space-y-1">
                  <span className="text-xs text-[var(--midnight-muted)]">Project</span>
                  <div className="max-h-32 space-y-0.5 overflow-y-auto">
                    {projects.map((project) => (
                      <label
                        key={project.id}
                        className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 hover:bg-[var(--cream-deep)]"
                      >
                        <Checkbox
                          checked={state.projects.includes(project.id)}
                          onCheckedChange={() =>
                            onChange({
                              projects: toggleIssueFilterValue(state.projects, project.id),
                            })
                          }
                        />
                        <span className="text-sm">{project.name}</span>
                      </label>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>

            <div className="min-w-0 space-y-3">
              {labels && labels.length > 0 ? (
                <div className="space-y-1">
                  <span className="text-xs text-[var(--midnight-muted)]">Labels</span>
                  <div className="max-h-32 space-y-0.5 overflow-y-auto">
                    {labels.map((label) => (
                      <label
                        key={label.id}
                        className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 hover:bg-[var(--cream-deep)]"
                      >
                        <Checkbox
                          checked={state.labels.includes(label.id)}
                          onCheckedChange={() =>
                            onChange({ labels: toggleIssueFilterValue(state.labels, label.id) })
                          }
                        />
                        <span
                          className="h-2.5 w-2.5 rounded-full"
                          style={{ backgroundColor: label.color }}
                        />
                        <span className="text-sm">{label.name}</span>
                      </label>
                    ))}
                  </div>
                </div>
              ) : null}

              {workspaces && workspaces.length > 0 ? (
                <div className="space-y-1">
                  <span className="text-xs text-[var(--midnight-muted)]">Workspace</span>
                  <div className="max-h-32 space-y-0.5 overflow-y-auto">
                    {workspaces.map((workspace) => (
                      <label
                        key={workspace.id}
                        className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 hover:bg-[var(--cream-deep)]"
                      >
                        <Checkbox
                          checked={state.workspaces.includes(workspace.id)}
                          onCheckedChange={() =>
                            onChange({
                              workspaces: toggleIssueFilterValue(state.workspaces, workspace.id),
                            })
                          }
                        />
                        <HardDrive size={16} className="text-[var(--midnight-muted)]" />
                        <span className="text-sm">{workspace.name}</span>
                      </label>
                    ))}
                  </div>
                </div>
              ) : null}

              <div className="space-y-1">
                <span className="text-xs text-[var(--midnight-muted)]">Visibility</span>
                <label className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 hover:bg-[var(--cream-deep)]">
                  <Checkbox
                    checked={state.liveOnly}
                    onCheckedChange={(checked) => onChange({ liveOnly: checked === true })}
                  />
                  <span className="text-sm">Live runs only</span>
                </label>
                {enableRoutineVisibilityFilter ? (
                  <label className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 hover:bg-[var(--cream-deep)]">
                    <Checkbox
                      checked={state.hideRoutineExecutions}
                      onCheckedChange={(checked) =>
                        onChange({ hideRoutineExecutions: checked === true })
                      }
                    />
                    <span className="text-sm">Hide routine runs</span>
                  </label>
                ) : null}
              </div>
            </div>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
