// apps/frontend/src/components/teams/shared/lib/searchQueryParser.ts
//
// Ported from upstream Paperclip (ui/src/lib/search-query-parser.ts,
// MIT © 2025 Paperclip AI). Parses a free-text search box into (a) the residual
// full-text query and (b) structured filters, driven by inline operator tokens
// (`status:todo`, `assignee:me`, `project:"Acme"`, `is:open`, `updated:>7d`).
//
// Isol8 adaptations: `status:` is validated against SEARCH_ISSUE_STATUSES — the
// exact set the `/teams/search` BFF (routers/teams/search.py `_ISSUE_STATUSES`)
// forwards to upstream, NOT the board-column `ISSUE_STATUSES` from shared/types.
// The priority + updated-within lists are inlined local, and the upstream
// `normalizeAgentUrlKey` dependency is dropped — entity lookup falls back to a
// plain lowercased name/id/urlKey compare. The URL-sync helpers (react-router
// `URLSearchParams` apply/read) are NOT ported; the SearchPanel owns query
// state in React and calls `buildSearchQueryString` for the BFF request.

import { type IssuePriority, type IssueStatus } from "@/components/teams/shared/types";

const ISSUE_PRIORITIES: readonly IssuePriority[] = ["critical", "high", "medium", "low"];

// The status values the `/teams/search` BFF accepts. MUST stay in lockstep with
// `_ISSUE_STATUSES` in apps/backend/routers/teams/search.py — the search index
// accepts backlog/cancelled and rejects pending/review/won_t_do/duplicate, so
// this deliberately differs from the board-column `ISSUE_STATUSES`. Validating
// `status:` against the board set instead would drop `status:backlog` /
// `status:cancelled` to literal text and emit `status:pending` (and friends) as
// filters the BFF 422s on ("Search failed").
const SEARCH_ISSUE_STATUSES: readonly IssueStatus[] = [
  "backlog",
  "todo",
  "in_progress",
  "in_review",
  "done",
  "blocked",
  "cancelled",
];

const OPEN_STATUSES: IssueStatus[] = ["backlog", "todo", "in_progress", "in_review", "blocked"];
const CLOSED_STATUSES: IssueStatus[] = ["done", "cancelled"];

const UPDATED_WITHIN_RE = /^[1-9]\d{0,2}(h|d|w|m)$/;
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

function isUuidLike(value: string): boolean {
  return UUID_RE.test(value.trim());
}

export type SearchOperatorKey =
  | "status"
  | "assignee"
  | "project"
  | "label"
  | "priority"
  | "updated"
  | "is";

export interface SearchOperatorPill {
  key: SearchOperatorKey;
  value: string;
  label: string;
}

export interface SearchOperatorSuggestion {
  token: string;
  label: string;
  description: string;
}

export const SEARCH_OPERATOR_SUGGESTIONS: SearchOperatorSuggestion[] = [
  { token: "status:todo", label: "Open todo tasks", description: "Filter by task status" },
  { token: "status:blocked", label: "Blocked tasks", description: "Find blocked work" },
  { token: "assignee:me", label: "Assigned to me", description: "Use your current board user" },
  { token: 'project:"Acme App"', label: "Project name", description: "Quote multi-word project names" },
  { token: "label:bug", label: "Label", description: "Filter by issue label" },
  { token: "priority:high", label: "High priority", description: "Filter by priority" },
  { token: "updated:>7d", label: "Recently updated", description: "Updated in the last 7 days" },
];

export interface SearchQueryParserContext {
  currentAgentId?: string | null;
  currentUserId?: string | null;
  agents?: readonly { id: string; name: string; urlKey?: string | null }[];
  projects?: readonly { id: string; name: string; urlKey?: string | null }[];
  labels?: readonly { id: string; name: string }[];
}

export interface SearchFilters {
  status?: IssueStatus[];
  priority?: IssuePriority[];
  assigneeAgentId?: string | null;
  assigneeUserId?: string;
  projectId?: string;
  labelId?: string;
  updatedWithin?: string;
  updatedAfter?: string;
}

export interface ParsedSearchQuery {
  query: string;
  filters: SearchFilters;
  pills: SearchOperatorPill[];
}

interface QueryToken {
  raw: string;
  value: string;
}

function stripValueQuotes(value: string): string {
  if (value.length >= 2 && value.startsWith('"') && value.endsWith('"')) {
    return value.slice(1, -1);
  }
  return value;
}

function tokenizeQuery(input: string): QueryToken[] {
  const tokens: QueryToken[] = [];
  let index = 0;
  while (index < input.length) {
    while (/\s/.test(input[index] ?? "")) index += 1;
    if (index >= input.length) break;

    const start = index;
    if (input[index] === '"') {
      index += 1;
      while (index < input.length && input[index] !== '"') index += 1;
      if (input[index] === '"') index += 1;
      const raw = input.slice(start, index);
      tokens.push({ raw, value: raw });
      continue;
    }

    while (index < input.length && !/\s/.test(input[index] ?? "")) {
      if (input[index] === ":" && input[index + 1] === '"') {
        index += 2;
        while (index < input.length && input[index] !== '"') index += 1;
        if (input[index] === '"') index += 1;
        break;
      }
      index += 1;
    }

    const raw = input.slice(start, index);
    tokens.push({ raw, value: raw });
  }
  return tokens;
}

function currentTokenBounds(input: string): { start: number; end: number; token: string } {
  let end = input.length;
  while (end > 0 && /\s/.test(input[end - 1] ?? "")) end -= 1;
  let start = end;
  while (start > 0 && !/\s/.test(input[start - 1] ?? "")) start -= 1;
  return { start, end, token: input.slice(start, end) };
}

export function searchOperatorSuggestions(input: string, limit = 5): SearchOperatorSuggestion[] {
  const { token } = currentTokenBounds(input);
  const normalized = token.toLowerCase();
  const candidates =
    normalized.length > 0
      ? SEARCH_OPERATOR_SUGGESTIONS.filter((s) => s.token.toLowerCase().startsWith(normalized))
      : SEARCH_OPERATOR_SUGGESTIONS;
  return candidates.slice(0, limit);
}

export function applySearchOperatorSuggestion(input: string, token: string): string {
  const { start, end } = currentTokenBounds(input);
  const prefix = input.slice(0, start).trimEnd();
  const suffix = input.slice(end).trimStart();
  return [prefix, token, suffix].filter(Boolean).join(" ").trim();
}

function normalizedLookup(value: string): string {
  return value.trim().toLowerCase();
}

function findByNameOrId<T extends { id: string; name: string; urlKey?: string | null }>(
  entries: readonly T[] | undefined,
  value: string,
): T | null {
  const normalized = normalizedLookup(value);
  return (
    entries?.find((entry) => {
      if (entry.id === value) return true;
      if (normalizedLookup(entry.name) === normalized) return true;
      return entry.urlKey ? normalizedLookup(entry.urlKey) === normalized : false;
    }) ?? null
  );
}

function addUnique<T extends string>(values: T[] | undefined, value: T): T[] {
  return values?.includes(value) ? values : [...(values ?? []), value];
}

function appendText(parts: string[], raw: string): void {
  if (raw.trim().length > 0) parts.push(raw);
}

function parseStatus(value: string): IssueStatus | null {
  return (SEARCH_ISSUE_STATUSES as readonly string[]).includes(value) ? (value as IssueStatus) : null;
}

function parsePriority(value: string): IssuePriority | null {
  return (ISSUE_PRIORITIES as readonly string[]).includes(value) ? (value as IssuePriority) : null;
}

function parseUpdatedWithin(value: string): string | null {
  const normalized = value.startsWith(">") ? value.slice(1) : value;
  if (!UPDATED_WITHIN_RE.test(normalized)) return null;
  return normalized;
}

function operatorLabel(key: SearchOperatorKey, value: string): string {
  return `${key}:${value}`;
}

export function parseSearchQuery(
  input: string,
  context: SearchQueryParserContext = {},
): ParsedSearchQuery {
  const textParts: string[] = [];
  const filters: SearchFilters = {};
  const pills: SearchOperatorPill[] = [];

  for (const token of tokenizeQuery(input)) {
    const match = /^([a-zA-Z]+):(.*)$/s.exec(token.value);
    if (!match) {
      appendText(textParts, token.raw);
      continue;
    }

    const key = match[1]!.toLowerCase();
    const rawValue = match[2]!;
    const value = stripValueQuotes(rawValue).trim();
    if (!value) {
      appendText(textParts, token.raw);
      continue;
    }

    if (key === "status") {
      const status = parseStatus(value);
      if (!status) {
        appendText(textParts, token.raw);
        continue;
      }
      filters.status = addUnique(filters.status, status);
      pills.push({ key: "status", value: status, label: operatorLabel("status", status) });
      continue;
    }

    if (key === "priority") {
      const priority = parsePriority(value);
      if (!priority) {
        appendText(textParts, token.raw);
        continue;
      }
      filters.priority = addUnique(filters.priority, priority);
      pills.push({ key: "priority", value: priority, label: operatorLabel("priority", priority) });
      continue;
    }

    if (key === "assignee") {
      if (value.toLowerCase() === "me") {
        if (context.currentAgentId) {
          filters.assigneeAgentId = context.currentAgentId;
          pills.push({ key: "assignee", value: "me", label: "assignee:me" });
          continue;
        }
        if (context.currentUserId) {
          filters.assigneeUserId = context.currentUserId;
          pills.push({ key: "assignee", value: "me", label: "assignee:me" });
          continue;
        }
        // No board identity resolved yet — forward the literal `me` sentinel;
        // the BFF/upstream resolves it against the session actor.
        filters.assigneeUserId = "me";
        pills.push({ key: "assignee", value: "me", label: "assignee:me" });
        continue;
      }

      const agent = findByNameOrId(context.agents, value);
      if (!agent) {
        appendText(textParts, token.raw);
        continue;
      }
      filters.assigneeAgentId = agent.id;
      pills.push({ key: "assignee", value: agent.name, label: operatorLabel("assignee", agent.name) });
      continue;
    }

    if (key === "project") {
      const project = findByNameOrId(context.projects, value);
      if (!project) {
        appendText(textParts, token.raw);
        continue;
      }
      filters.projectId = project.id;
      pills.push({ key: "project", value: project.name, label: operatorLabel("project", project.name) });
      continue;
    }

    if (key === "label") {
      const label = findByNameOrId(context.labels, value);
      if (label) {
        filters.labelId = label.id;
        pills.push({ key: "label", value: label.name, label: operatorLabel("label", label.name) });
        continue;
      }
      if (isUuidLike(value)) {
        filters.labelId = value;
        pills.push({ key: "label", value, label: operatorLabel("label", value.slice(0, 8)) });
        continue;
      }
      appendText(textParts, token.raw);
      continue;
    }

    if (key === "updated") {
      const updatedWithin = parseUpdatedWithin(value);
      if (!updatedWithin) {
        appendText(textParts, token.raw);
        continue;
      }
      filters.updatedWithin = updatedWithin;
      pills.push({
        key: "updated",
        value: `>${updatedWithin}`,
        label: operatorLabel("updated", `>${updatedWithin}`),
      });
      continue;
    }

    if (key === "is") {
      if (value === "open") {
        filters.status = OPEN_STATUSES;
        pills.push({ key: "is", value: "open", label: "is:open" });
        continue;
      }
      if (value === "closed") {
        filters.status = CLOSED_STATUSES;
        pills.push({ key: "is", value: "closed", label: "is:closed" });
        continue;
      }
      appendText(textParts, token.raw);
      continue;
    }

    appendText(textParts, token.raw);
  }

  return {
    query: textParts.join(" ").replace(/\s+/g, " ").trim(),
    filters,
    pills,
  };
}

export function hasSearchFilters(filters: SearchFilters): boolean {
  return Boolean(
    filters.status?.length ||
      filters.priority?.length ||
      filters.assigneeAgentId !== undefined ||
      filters.assigneeUserId ||
      filters.projectId ||
      filters.labelId ||
      filters.updatedWithin ||
      filters.updatedAfter,
  );
}

// Build the query string appended to `/teams/search` for the BFF request.
// Multi-value filters (status/priority) repeat the key so the BFF forwards a
// list to upstream. Excludes scope/sort — those are panel-level UI state the
// SearchPanel appends itself.
export function buildSearchQueryString(query: string, filters: SearchFilters): string {
  const params = new URLSearchParams();
  const trimmed = query.trim();
  if (trimmed) params.set("q", trimmed);
  for (const status of filters.status ?? []) params.append("status", status);
  for (const priority of filters.priority ?? []) params.append("priority", priority);
  if (filters.assigneeAgentId !== undefined) {
    params.set("assigneeAgentId", filters.assigneeAgentId ?? "null");
  }
  if (filters.assigneeUserId) params.set("assigneeUserId", filters.assigneeUserId);
  if (filters.projectId) params.set("projectId", filters.projectId);
  if (filters.labelId) params.set("labelId", filters.labelId);
  if (filters.updatedWithin) params.set("updatedWithin", filters.updatedWithin);
  if (filters.updatedAfter) params.set("updatedAfter", filters.updatedAfter);
  return params.toString();
}
