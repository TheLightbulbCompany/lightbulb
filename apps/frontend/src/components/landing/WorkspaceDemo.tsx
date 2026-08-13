"use client";

/**
 * The landing page's live workspace demo: the REAL /chat components (Teams
 * TasksList, InboxList, IssueHeader/IssueComments, dashboard MetricCard, chat
 * MessageList, shell PanelMasthead/PanelToolbar) rendering canned
 * ceramics-studio data with pure props — no providers, no fetches, no auth.
 * Row clicks are captured by an InlineTeamsNavProvider so detail views open
 * inside the demo frame instead of navigating to /chat.
 *
 * Composition mirrors the real desktop workspace shell: 280px cream sidebar
 * (roster + CompanyNav + footer), slim white header row, and a three-panel
 * dock — Friday chat left, Dashboard right-top, Inbox right-bottom — with
 * dockview-style tab strips faked in CSS (no dockview dep).
 *
 * Deliberate lookalikes (the real ones reach for live wiring): the composer
 * (ChatInput needs useGateway), the sidebar (static markup over
 * ChatLayout.css's .cream-sidebar recipe), the dashboard's active-run card
 * (ActiveAgentsPanel polls the run log), and the CSS-grid dock.
 */

import * as React from "react";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import {
  ArrowUp,
  Bot,
  ChevronDown,
  CircleDot,
  FolderKanban,
  FolderOpen,
  HelpCircle,
  History,
  Images,
  Inbox,
  LayoutDashboard,
  ListTodo,
  Network,
  Pause,
  Plus,
  Repeat,
  RotateCcw,
  Search,
  Settings,
  SlidersHorizontal,
  Target,
  User,
  X,
} from "lucide-react";
import "@/components/chat/ChatLayout.css";

import { InlineTeamsNavProvider } from "@/components/chat/InlineTeamsNav";
import { Monogram } from "@/components/agents/AgentIdentity";
import {
  EditorialButton,
  PanelMasthead,
  PanelToolbar,
} from "@/components/teams/shell";
import { MetricCard } from "@/components/teams/dashboard/MetricCard";
import { TasksList } from "@/components/teams/issues/TasksList";
import { IssueHeader } from "@/components/teams/issues/IssueHeader";
import { IssueComments } from "@/components/teams/issues/IssueComments";
import { InboxList } from "@/components/teams/inbox/InboxList";
import {
  buildGroupedInboxSections,
  getInboxWorkItems,
  matchesInboxIssueSearch,
} from "@/components/teams/shared/lib/inbox";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { PriorityIcon } from "@/components/teams/shared/components/PriorityIcon";
import { Identity } from "@/components/teams/shared/components/Identity";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { LiveRunDot } from "@/components/teams/shared/components/LiveRunDot";
import type { Issue, IssueComment } from "@/components/teams/shared/types";
import {
  DEMO_AGENTS,
  DEMO_AGENT_LOOKS,
  DEMO_COMMENTS,
  DEMO_COMPANY_NAME,
  DEMO_ISSUES,
  DEMO_LIVE_ISSUE_IDS,
  DEMO_MODEL_NAME,
  DEMO_NOW_ISO,
  DEMO_RENDER_GROUPS,
  FRIDAY_ID,
} from "./demoData";

// MessageList statically imports Prism + react-markdown — keep it out of the
// landing page's first-load JS and off the server render.
const MessageList = dynamic(
  () => import("@/components/chat/MessageList").then((m) => m.MessageList),
  { ssr: false, loading: () => <div className="flex-1 min-h-0" /> },
);

type DemoView = "chat" | "tasks" | "dashboard" | "inbox";
type InboxTab = "mine" | "recent" | "unread" | "all";

const EMPTY_SET = new Set<string>();

const INBOX_TABS: { value: InboxTab; label: string }[] = [
  { value: "mine", label: "Mine" },
  { value: "recent", label: "Recent" },
  { value: "unread", label: "Unread" },
  { value: "all", label: "All" },
];

/** Sidebar nav row — same recipe as CompanyNav's buttons. */
function NavRow({
  active,
  Icon,
  label,
  trailing,
  onClick,
}: {
  active?: boolean;
  Icon: React.ComponentType<{ size?: number }>;
  label: string;
  trailing?: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 rounded-full px-3 py-2 text-sm transition-colors ${
        active
          ? "bg-[var(--cream)] text-[var(--forest)]"
          : "text-[var(--ink-soft)] hover:bg-[var(--forest-tint)] hover:text-[var(--forest)]"
      }`}
    >
      <Icon size={16} />
      <span className="flex-1 text-left">{label}</span>
      {trailing}
    </button>
  );
}

function PropertyRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-3 py-1.5">
      <span className="w-20 shrink-0 text-xs text-[var(--mute)]">{label}</span>
      <span className="flex min-w-0 items-center gap-1.5 text-sm text-[var(--ink)]">
        {children}
      </span>
    </div>
  );
}

/** Dockview-style tab strip (single tab per group, like the real default layout). */
function TabStrip({ label }: { label: string }) {
  return (
    <div className="ld-tabstrip">
      <span className="ld-tab">
        {label}
        <X size={12} className="ld-tab-x" aria-hidden="true" />
      </span>
    </div>
  );
}

export function WorkspaceDemo() {
  const router = useRouter();
  const [view, setView] = React.useState<DemoView>("chat");
  const [selectedAgentId, setSelectedAgentId] = React.useState(FRIDAY_ID);
  const [detailIssueId, setDetailIssueId] = React.useState<string | null>(null);
  const [issues, setIssues] = React.useState<Issue[]>(DEMO_ISSUES);
  const [comments, setComments] =
    React.useState<Record<string, IssueComment[]>>(DEMO_COMMENTS);
  const [taskSearch, setTaskSearch] = React.useState("");
  const [inboxSearch, setInboxSearch] = React.useState("");
  const [inboxTab, setInboxTab] = React.useState<InboxTab>("all");
  const [composerDraft, setComposerDraft] = React.useState("");

  const updateIssue = React.useCallback(
    (id: string, data: Record<string, unknown>) => {
      setIssues((prev) =>
        prev.map((issue) => (issue.id === id ? { ...issue, ...data } : issue)),
      );
    },
    [],
  );

  const nav = React.useMemo(
    () => ({
      openDetail: (_panelKey: string, id: string) => setDetailIssueId(id),
      leaveToList: () => setDetailIssueId(null),
    }),
    [],
  );

  const selectedAgent =
    DEMO_AGENTS.find((a) => a.id === selectedAgentId) ?? DEMO_AGENTS[0];
  const detailIssue = detailIssueId
    ? (issues.find((i) => i.id === detailIssueId) ?? null)
    : null;

  // Dismissed-from-inbox rows: locally removed, exactly like the real inbox.
  const [archivedIds, setArchivedIds] = React.useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const archiveIssue = React.useCallback((id: string) => {
    setArchivedIds((prev) => new Set(prev).add(id));
  }, []);

  // "Hide" from a task row patches hiddenAt (see issues/lib/hideIssue.ts) —
  // the demo must honor it or the action appears broken.
  const searchedIssues = React.useMemo(
    () =>
      issues.filter(
        (i) => !i.hiddenAt && matchesInboxIssueSearch(i, taskSearch),
      ),
    [issues, taskSearch],
  );

  const inboxIssues = React.useMemo(
    () =>
      issues.filter(
        (i) =>
          !archivedIds.has(i.id) &&
          !i.hiddenAt &&
          (inboxTab !== "unread" || i.unread) &&
          matchesInboxIssueSearch(i, inboxSearch),
      ),
    [issues, archivedIds, inboxTab, inboxSearch],
  );
  const inboxSections = React.useMemo(
    () =>
      buildGroupedInboxSections(getInboxWorkItems(inboxIssues), {
        nowIso: DEMO_NOW_ISO,
      }),
    [inboxIssues],
  );
  const unreadCount = issues.filter(
    (i) => i.unread && !archivedIds.has(i.id) && !i.hiddenAt,
  ).length;
  const openTaskCount = issues.filter(
    (i) => !i.hiddenAt && i.status !== "done" && i.status !== "cancelled",
  ).length;
  const runningAgents = DEMO_AGENTS.filter((a) => a.status === "running").length;

  const openView = (next: DemoView) => {
    setView(next);
    setDetailIssueId(null);
  };

  const assigneeOf = (issue: Issue) =>
    DEMO_AGENTS.find((a) => a.id === issue.assigneeAgentId) ?? null;

  const namingIssue = issues.find((i) => i.id === "iss-name");

  // <1000px the dock shows ONE panel; this attribute picks which.
  const pane =
    detailIssue || view === "chat" || view === "tasks" ? "left" : view;
  const leftTabLabel = detailIssue
    ? (detailIssue.identifier ?? "Task")
    : view === "tasks"
      ? "Tasks"
      : selectedAgent.name;

  /* ── panel contents ── */

  const chatPanel = (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-1 border-b border-[var(--edge)] px-3">
        <span className="truncate text-sm font-medium text-[var(--ink)]">
          {selectedAgent.name}
        </span>
        {selectedAgent.status === "running" && <LiveRunDot />}
        <div className="flex-1" />
        {/* Agent quick actions — visual parity with ChatPanel's header cluster;
            the real ones open the inspector / tasks / pause the agent. */}
        <span className="flex items-center gap-1 text-[var(--mute)]" aria-hidden="true">
          <SlidersHorizontal size={16} className="p-px" />
          <ListTodo size={16} className="p-px" />
          <Pause size={16} className="p-px" />
        </span>
      </div>
      {selectedAgent.id === FRIDAY_ID ? (
        <MessageList
          renderGroups={DEMO_RENDER_GROUPS}
          agentName={selectedAgent.name}
        />
      ) : (
        <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-[var(--mute)]">
          {selectedAgent.name} is {selectedAgent.status ?? "idle"} — nothing in
          this chat yet.
        </div>
      )}
      {/* Composer lookalike over ChatInput's card recipe — the real one needs
          the gateway; here, sending routes into signup. */}
      <form
        className="px-4 pb-4 pt-1"
        onSubmit={(e) => {
          e.preventDefault();
          router.push("/sign-up");
        }}
      >
        <div className="flex flex-col rounded-2xl border border-[var(--edge)] bg-white shadow-[0_1px_2px_rgba(26,26,26,0.03),0_4px_16px_rgba(26,26,26,0.05)] transition-[border-color] focus-within:border-[var(--forest)]/25">
          <input
            value={composerDraft}
            onChange={(e) => setComposerDraft(e.target.value)}
            placeholder="Ask anything"
            aria-label={`Message ${selectedAgent.name}`}
            className="w-full bg-transparent px-4 pt-3 text-[15px] leading-6 text-[var(--ink)] placeholder:text-[#b5ae9e] focus:outline-none"
          />
          <div className="flex items-center gap-1 px-2 pb-2 pt-1.5">
            <span
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[var(--mute)]"
              aria-hidden="true"
            >
              <Plus className="h-4 w-4" />
            </span>
            <span className="flex items-center gap-1 rounded-full px-2 py-1 text-xs text-[var(--mute)]">
              {DEMO_MODEL_NAME}
              <ChevronDown className="h-3 w-3" aria-hidden="true" />
            </span>
            <button
              type="submit"
              aria-label="Send"
              className="ml-auto flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--forest)] text-white transition-colors hover:bg-[#0a5c3e]"
            >
              <ArrowUp className="h-4 w-4" />
            </button>
          </div>
        </div>
      </form>
    </div>
  );

  const tasksPanel = (
    <div className="min-h-0 flex-1 overflow-y-auto p-5 sm:p-6">
      <PanelMasthead overline="Work" title="Tasks" />
      <PanelToolbar
        className="mt-4"
        searchValue={taskSearch}
        onSearchChange={setTaskSearch}
        searchPlaceholder="Search tasks…"
      />
      <div className="mt-3">
        <TasksList
          issues={searchedIssues}
          agents={DEMO_AGENTS}
          liveIssueIds={DEMO_LIVE_ISSUE_IDS}
          searchActive={taskSearch.trim().length > 0}
          onUpdateIssue={updateIssue}
          // Creating tasks is the product — the demo's "New task" is a
          // signup door, same as the composer.
          onNewTask={() => router.push("/sign-up")}
        />
      </div>
    </div>
  );

  const detailPanel = detailIssue ? (
    <div className="flex min-h-0 flex-1">
      <div className="min-w-0 flex-1 overflow-y-auto p-5 sm:p-6">
        <button
          type="button"
          className="mb-4 text-xs text-[var(--mute)] transition-colors hover:text-[var(--ink)]"
          onClick={() => setDetailIssueId(null)}
        >
          ← Back
        </button>
        <IssueHeader
          issue={detailIssue}
          onTitleSave={async (title) => updateIssue(detailIssue.id, { title })}
        />
        {detailIssue.description ? (
          <div className="mt-4 text-sm text-[var(--ink-soft)]">
            <MarkdownView>{detailIssue.description}</MarkdownView>
          </div>
        ) : null}
        <div className="mt-8 border-t border-[var(--edge)] pt-5">
          <IssueComments
            comments={comments[detailIssue.id] ?? []}
            agents={DEMO_AGENTS}
            onSubmit={async (body) => {
              const comment: IssueComment = {
                id: `cmt-you-${Date.now()}`,
                body,
                createdAt: new Date().toISOString(),
                authorKind: "user",
                authorName: "You",
              };
              setComments((prev) => ({
                ...prev,
                [detailIssue.id]: [...(prev[detailIssue.id] ?? []), comment],
              }));
            }}
          />
        </div>
      </div>
      {/* properties rail — static rows, real icon components */}
      <div className="hidden w-56 shrink-0 overflow-y-auto border-l border-[var(--edge)] p-4 lg:block">
        <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.12em] text-[var(--mute)]">
          Properties
        </p>
        <PropertyRow label="Status">
          <StatusIcon
            status={detailIssue.status}
            showLabel
            onChange={(status) => updateIssue(detailIssue.id, { status })}
          />
        </PropertyRow>
        <PropertyRow label="Assignee">
          {assigneeOf(detailIssue) ? (
            <Identity name={assigneeOf(detailIssue)!.name} size="sm" />
          ) : (
            <span className="text-xs text-[var(--mute)]">Unassigned</span>
          )}
        </PropertyRow>
        {detailIssue.priority ? (
          <PropertyRow label="Priority">
            <PriorityIcon priority={detailIssue.priority} showLabel />
          </PropertyRow>
        ) : null}
        {detailIssue.project ? (
          <PropertyRow label="Project">{detailIssue.project.name}</PropertyRow>
        ) : null}
        {(detailIssue.labels?.length ?? 0) > 0 ? (
          <PropertyRow label="Labels">
            {detailIssue.labels!.map((label) => (
              <span
                key={label.id}
                className="rounded border border-[var(--edge)] bg-[var(--cream-deeper)] px-1.5 py-0.5 text-[11px] text-[var(--ink-soft)]"
              >
                {label.name}
              </span>
            ))}
          </PropertyRow>
        ) : null}
      </div>
    </div>
  ) : null;

  const dashboardPanel = (
    <div className="min-h-0 flex-1 overflow-y-auto p-5 sm:p-6">
      <PanelMasthead overline="Operations" title="Dashboard" meta="Last 14 days" />
      <section aria-label="Active agents" className="mt-4 min-w-0">
        <h2 className="mb-2 font-editorial-serif text-2xl text-[var(--ink)]">
          Agents
        </h2>
        {/* Active-run card lookalike (the real ActiveAgentsPanel polls the
            run log): Friday live on the naming task. */}
        <div
          className="editorial-card flex flex-col gap-2 overflow-hidden p-3"
          style={{
            borderColor: "color-mix(in srgb, var(--forest) 35%, var(--edge))",
            background:
              "color-mix(in srgb, var(--forest-tint) 55%, var(--cream))",
          }}
        >
          <div className="flex items-start justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <LiveRunDot />
              <Identity name="Friday" size="sm" />
            </div>
            <span className="shrink-0 text-[11px] text-[var(--mute)]">
              Details
            </span>
          </div>
          <div className="text-[11px] text-[var(--mute)]">Live now</div>
          {namingIssue ? (
            <button
              type="button"
              onClick={() => setDetailIssueId(namingIssue.id)}
              className="block rounded-lg border border-[var(--edge)] bg-[var(--cream-deeper)] px-2.5 py-2 text-left text-xs text-[var(--forest)] transition-opacity hover:opacity-80"
            >
              <span className="line-clamp-2">
                <span className="font-medium">{namingIssue.identifier}</span>
                <span> — {namingIssue.title}</span>
              </span>
            </button>
          ) : null}
          <div
            className="rounded-lg px-2.5 py-2 font-mono text-[10px] leading-relaxed"
            style={{
              background: "var(--cream-deepest)",
              color: "var(--ink-soft)",
              borderTop: "1px solid var(--edge)",
            }}
            aria-label="Live agent activity"
          >
            <div>$ whois northwindceramics.com — unregistered</div>
            <div>Claiming @northwindceramics on Instagram + TikTok…</div>
          </div>
        </div>
      </section>
      <section
        aria-label="Key metrics"
        className="editorial-card mt-4 grid grid-cols-2 divide-x divide-[var(--edge)] overflow-hidden"
      >
        <MetricCard
          icon={Bot}
          value={DEMO_AGENTS.length}
          label="Agents"
          description={
            <span>{runningAgents} running, 0 paused</span>
          }
        />
        <MetricCard
          icon={CircleDot}
          value={openTaskCount}
          label="Open Tasks"
          description={<span>{openTaskCount} open, 0 blocked</span>}
        />
      </section>
    </div>
  );

  const inboxPanel = (
    <div className="min-h-0 flex-1 overflow-y-auto p-5 sm:p-6">
      <PanelMasthead
        overline="Inbox"
        title="Inbox"
        meta={unreadCount > 0 ? `${unreadCount} unread` : undefined}
      />
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <div
          role="tablist"
          aria-label="Inbox tabs"
          className="inline-flex items-center gap-1 rounded-md bg-[var(--cream-deeper)] p-0.5"
        >
          {INBOX_TABS.map((t) => {
            const isActive = inboxTab === t.value;
            return (
              <button
                key={t.value}
                type="button"
                role="tab"
                aria-selected={isActive}
                onClick={() => setInboxTab(t.value)}
                className="h-7 rounded-sm px-3 text-xs font-medium transition-colors"
                style={
                  isActive
                    ? { background: "var(--forest-tint)", color: "var(--forest)" }
                    : { color: "var(--mute)" }
                }
              >
                {t.label}
              </button>
            );
          })}
        </div>
        <PanelToolbar
          className="min-w-[340px] flex-1"
          searchValue={inboxSearch}
          onSearchChange={setInboxSearch}
          searchPlaceholder="Search inbox..."
          searchAriaLabel="Search inbox"
          filters={
            <EditorialButton
              type="button"
              variant="secondary"
              size="icon"
              aria-label="Filters"
            >
              <SlidersHorizontal className="h-3.5 w-3.5" />
            </EditorialButton>
          }
          actions={
            <>
              <EditorialButton
                type="button"
                variant="primary"
                size="sm"
                className="shrink-0"
                onClick={() => router.push("/sign-up")}
              >
                <Plus className="h-3 w-3" />
                <span>New task</span>
              </EditorialButton>
              <EditorialButton
                type="button"
                variant="secondary"
                size="sm"
                className="shrink-0"
                disabled={unreadCount === 0}
                onClick={() => {
                  for (const i of issues) {
                    if (i.unread) updateIssue(i.id, { unread: false });
                  }
                }}
              >
                Mark all as read
              </EditorialButton>
            </>
          }
        />
      </div>
      <div className="editorial-card mt-4 overflow-hidden">
        <InboxList
          sections={inboxSections}
          selectedIssueId={detailIssueId}
          onSelect={(id) => setDetailIssueId(id)}
          onArchive={(id) => {
            archiveIssue(id);
            updateIssue(id, { unread: false });
          }}
          onMarkRead={(id) => updateIssue(id, { unread: false })}
          archivingIds={EMPTY_SET}
          statusMenu={(issue) => (
            <span className="flex items-center gap-1.5 text-xs text-[var(--mute)]">
              <StatusIcon status={issue.status} />
            </span>
          )}
        />
      </div>
    </div>
  );

  return (
    <section className="ld-demo" id="workspace">
      <div className="ld-section">
        <div className="ld-kicker">the workspace</div>
        <h2 className="ld-h2">
          This is what <em>running one</em> looks like.
        </h2>
        <p className="ld-lede">
          Your company lives in one workspace: talk to Friday, watch the task
          board move, and step in wherever you want a say. This one is real —
          click around.
        </p>

        <InlineTeamsNavProvider value={nav}>
          <div className="ld-demo-window" aria-label="Isol8 workspace demo">
            <div className="ld-demo-bar">
              <span className="ld-demo-dot" />
              <span className="ld-demo-dot" />
              <span className="ld-demo-dot" />
              <p className="ld-demo-title">
                isol8 — {DEMO_COMPANY_NAME.toLowerCase()}
              </p>
            </div>

            {/* ≤720px the sidebar is hidden — this strip is then the only
                way to switch panels, so it must mirror openView exactly. */}
            <div className="ld-demo-mobilenav" role="tablist" aria-label="Workspace demo views">
              {(["chat", "dashboard", "inbox", "tasks"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  role="tab"
                  aria-selected={view === v}
                  className={view === v ? "on" : undefined}
                  onClick={() => openView(v)}
                >
                  {v[0].toUpperCase() + v.slice(1)}
                </button>
              ))}
            </div>

            <div className="ld-demo-body">
              {/* ── sidebar (lookalike over .cream-sidebar classes) ── */}
              <aside className="cream-sidebar ld-demo-sidebar">
                <div className="sidebar-header">
                  <div className="sidebar-logo">
                    <span className="brand-logo brand-logo--ink brand-logo--sm">
                      ISOL8
                    </span>
                  </div>
                  <span className="sidebar-settings-link" aria-hidden="true">
                    <Settings size={16} />
                  </span>
                </div>
                <div className="flex items-center gap-2 px-3 py-2 text-xs text-[var(--mute)]">
                  <span className="h-2 w-2 shrink-0 rounded-full bg-[#2d8a4e]" />
                  Connected
                </div>
                <div className="create-pill" role="group" aria-label="Create">
                  <button
                    type="button"
                    className="create-pill__seg"
                    aria-label="New agent"
                    onClick={() => router.push("/sign-up")}
                  >
                    <Plus size={13} />
                    Agent
                  </button>
                  <span className="create-pill__divider" aria-hidden="true" />
                  <button
                    type="button"
                    className="create-pill__seg"
                    aria-label="New task"
                    onClick={() => router.push("/sign-up")}
                  >
                    <Plus size={13} />
                    Task
                  </button>
                </div>
                <div className="sidebar-scroll">
                  <div className="agent-list">
                    <div className="agent-section-label">
                      <span>My Agents</span>
                      <b>{DEMO_AGENTS.length}</b>
                    </div>
                    {DEMO_AGENTS.map((agent) => {
                      const look = DEMO_AGENT_LOOKS[agent.id];
                      const active =
                        view === "chat" && selectedAgentId === agent.id;
                      return (
                        <div
                          key={agent.id}
                          role="button"
                          tabIndex={0}
                          className={`agent-item${active ? " active" : ""}`}
                          onClick={() => {
                            setSelectedAgentId(agent.id);
                            openView("chat");
                          }}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              setSelectedAgentId(agent.id);
                              openView("chat");
                            }
                          }}
                        >
                          <Monogram agent={look} size={28} radius={8} />
                          <div className="agent-info">
                            <div className="agent-name">{agent.name}</div>
                            <div className="agent-model">{DEMO_MODEL_NAME}</div>
                          </div>
                          <span className="status-dot status-dot--active" aria-hidden="true" />
                          {selectedAgentId === agent.id && (
                            <div className="session-list">
                              <div className="session-row">
                                <button
                                  type="button"
                                  className="session-item active"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    openView("chat");
                                  }}
                                >
                                  <span className="session-label">General</span>
                                </button>
                              </div>
                              <button
                                type="button"
                                className="session-new"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  router.push("/sign-up");
                                }}
                              >
                                <Plus size={12} />
                                New session
                              </button>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                  {/* CompanyNav lookalike — same groups as the real sidebar.
                      Panels the demo cans are wired; the rest are signup
                      doors, like every other create-shaped action here. */}
                  <nav className="px-2 py-1" aria-label="Company">
                    <NavRow
                      Icon={LayoutDashboard}
                      label="Dashboard"
                      active={view === "dashboard"}
                      onClick={() => openView("dashboard")}
                    />
                    <NavRow
                      Icon={Search}
                      label="Search"
                      onClick={() => router.push("/sign-up")}
                    />
                    <NavRow
                      Icon={Inbox}
                      label="Inbox"
                      active={view === "inbox"}
                      trailing={
                        unreadCount > 0 ? (
                          <span className="rounded-full bg-[var(--forest)] px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white">
                            {unreadCount}
                          </span>
                        ) : undefined
                      }
                      onClick={() => openView("inbox")}
                    />
                    <div className="agent-section-label">
                      <span>Work</span>
                    </div>
                    <NavRow
                      Icon={ListTodo}
                      label="Tasks"
                      active={view === "tasks"}
                      onClick={() => openView("tasks")}
                    />
                    <NavRow
                      Icon={Repeat}
                      label="Automations"
                      onClick={() => router.push("/sign-up")}
                    />
                    <NavRow
                      Icon={Target}
                      label="Goals"
                      onClick={() => router.push("/sign-up")}
                    />
                    <NavRow
                      Icon={FolderKanban}
                      label="Projects"
                      onClick={() => router.push("/sign-up")}
                    />
                    <div className="agent-section-label">
                      <span>Company</span>
                    </div>
                    <NavRow
                      Icon={Network}
                      label="Org chart"
                      onClick={() => router.push("/sign-up")}
                    />
                    <NavRow
                      Icon={Images}
                      label="Artifacts"
                      onClick={() => router.push("/sign-up")}
                    />
                    <NavRow
                      Icon={History}
                      label="Activity"
                      onClick={() => router.push("/sign-up")}
                    />
                  </nav>
                  <div className="mt-3 border-t border-[var(--edge)] px-3 pb-2 pt-3">
                    <div className="font-editorial-serif px-1 text-[13.5px] italic text-[#5b5b5b]">
                      Marketplace
                    </div>
                  </div>
                </div>
                <div className="sidebar-footer">
                  <div className="user-row">
                    <div className="user-avatar">NC</div>
                    <div className="user-info">
                      <div className="user-name">{DEMO_COMPANY_NAME}</div>
                    </div>
                    <span className="plan-badge">Free Tier</span>
                  </div>
                  <div className="version-text">Isol8 v0.1</div>
                </div>
              </aside>

              {/* ── main column: white header row + three-panel dock ── */}
              <div className="ld-demo-main">
                <div className="ld-demo-topbar" aria-hidden="true">
                  <span className="ld-topbar-icon">
                    <FolderOpen size={17} />
                  </span>
                  <span className="ld-topbar-icon">
                    <RotateCcw size={17} />
                  </span>
                  <span className="flex-1" />
                  <span className="ld-topbar-icon">
                    <HelpCircle size={17} />
                  </span>
                  <span
                    className="ml-1 flex h-7 w-7 items-center justify-center rounded-full text-white"
                    style={{
                      background: "linear-gradient(135deg,#a78bfa,#6d28d9)",
                    }}
                  >
                    <User size={15} />
                  </span>
                </div>
                <div className="ld-dock" data-pane={pane}>
                  <div
                    className="ld-panel ld-panel--left"
                    data-testid="demo-left-panel"
                  >
                    <TabStrip label={leftTabLabel} />
                    <div className="ld-panel-body">
                      {detailIssue
                        ? detailPanel
                        : view === "tasks"
                          ? tasksPanel
                          : chatPanel}
                    </div>
                  </div>
                  <div className="ld-panel ld-panel--dash">
                    <TabStrip label="Dashboard" />
                    <div className="ld-panel-body">{dashboardPanel}</div>
                  </div>
                  <div className="ld-panel ld-panel--inbox">
                    <TabStrip label="Inbox" />
                    <div className="ld-panel-body">{inboxPanel}</div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </InlineTeamsNavProvider>
      </div>
    </section>
  );
}
