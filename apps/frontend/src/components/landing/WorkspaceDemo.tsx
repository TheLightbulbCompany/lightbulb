"use client";

/**
 * The landing page's live workspace demo: the REAL /chat components (Teams
 * TasksList/KanbanBoard, InboxList, IssueHeader/IssueComments, chat
 * MessageList) rendering canned ceramics-studio data with pure props — no
 * providers, no fetches, no auth. Row clicks are captured by an
 * InlineTeamsNavProvider so detail views open inside the demo frame instead
 * of navigating to /chat.
 *
 * Deliberate lookalikes (the real ones reach for live wiring): the composer
 * (ChatInput needs useGateway), the properties rail (IssueProperties fires
 * useTeamsApi reads), the sidebar (a static ~80-liner over ChatLayout.css's
 * .cream-sidebar classes), and a flexbox frame instead of dockview.
 */

import * as React from "react";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { Inbox, ListChecks, Send, Settings } from "lucide-react";
import "@/components/chat/ChatLayout.css";

import { InlineTeamsNavProvider } from "@/components/chat/InlineTeamsNav";
import { PanelMasthead, PanelToolbar } from "@/components/teams/shell";
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
  DEMO_COMMENTS,
  DEMO_COMPANY_NAME,
  DEMO_ISSUES,
  DEMO_LIVE_ISSUE_IDS,
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

type DemoView = "chat" | "tasks" | "inbox";

const EMPTY_SET = new Set<string>();

/** Sidebar row — reuses ChatLayout.css's .agent-item recipe. */
function SidebarRow({
  active,
  avatar,
  avatarBg,
  name,
  sub,
  trailing,
  onClick,
}: {
  active: boolean;
  avatar: React.ReactNode;
  avatarBg?: string;
  name: string;
  sub?: string;
  trailing?: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={`agent-item w-full text-left${active ? " active" : ""}`}
      onClick={onClick}
    >
      <span
        className="agent-avatar text-[13px] font-semibold text-white"
        style={avatarBg ? { background: avatarBg } : undefined}
        aria-hidden="true"
      >
        {avatar}
      </span>
      <span className="agent-info">
        <span className="agent-name block">{name}</span>
        {sub ? <span className="agent-model block">{sub}</span> : null}
      </span>
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

export function WorkspaceDemo() {
  const router = useRouter();
  const [view, setView] = React.useState<DemoView>("chat");
  const [selectedAgentId, setSelectedAgentId] = React.useState(FRIDAY_ID);
  const [detailIssueId, setDetailIssueId] = React.useState<string | null>(null);
  const [issues, setIssues] = React.useState<Issue[]>(DEMO_ISSUES);
  const [comments, setComments] =
    React.useState<Record<string, IssueComment[]>>(DEMO_COMMENTS);
  const [taskSearch, setTaskSearch] = React.useState("");
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
    () => issues.filter((i) => !archivedIds.has(i.id) && !i.hiddenAt),
    [issues, archivedIds],
  );
  const inboxSections = React.useMemo(
    () =>
      buildGroupedInboxSections(getInboxWorkItems(inboxIssues), {
        nowIso: DEMO_NOW_ISO,
      }),
    [inboxIssues],
  );
  const unreadCount = inboxIssues.filter((i) => i.unread).length;

  const openView = (next: DemoView) => {
    setView(next);
    setDetailIssueId(null);
  };

  const assigneeOf = (issue: Issue) =>
    DEMO_AGENTS.find((a) => a.id === issue.assigneeAgentId) ?? null;

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
                way to reach tasks/inbox, so it must mirror openView exactly. */}
            <div className="ld-demo-mobilenav" role="tablist" aria-label="Workspace demo views">
              {(["chat", "tasks", "inbox"] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  role="tab"
                  aria-selected={view === v}
                  className={view === v ? "on" : undefined}
                  onClick={() => openView(v)}
                >
                  {v === "chat" ? "Chat" : v === "tasks" ? "Tasks" : "Inbox"}
                </button>
              ))}
            </div>

            <div className="ld-demo-body">
              {/* ── sidebar (lookalike over .cream-sidebar classes) ── */}
              <aside className="cream-sidebar ld-demo-sidebar">
                <div className="sidebar-header">
                  <div className="sidebar-logo">
                    <span className="font-editorial-serif text-lg text-[var(--ink)]">
                      isol8
                    </span>
                  </div>
                  <span className="sidebar-settings-link" aria-hidden="true">
                    <Settings size={16} />
                  </span>
                </div>
                <div className="sidebar-scroll">
                  <div className="agent-list">
                    <div className="agent-section-label">Your team</div>
                    {DEMO_AGENTS.map((agent) => (
                      <SidebarRow
                        key={agent.id}
                        active={view === "chat" && selectedAgentId === agent.id}
                        avatar={agent.name[0]}
                        name={agent.name}
                        sub={agent.title ?? undefined}
                        trailing={
                          agent.status === "running" ? (
                            <span className="agent-status-dot" />
                          ) : undefined
                        }
                        onClick={() => {
                          setSelectedAgentId(agent.id);
                          openView("chat");
                        }}
                      />
                    ))}
                    <div className="agent-section-label">Company</div>
                    <SidebarRow
                      active={view === "tasks"}
                      avatar={<ListChecks size={16} />}
                      avatarBg="var(--forest)"
                      name="Tasks"
                      onClick={() => openView("tasks")}
                    />
                    <SidebarRow
                      active={view === "inbox"}
                      avatar={<Inbox size={16} />}
                      avatarBg="var(--forest)"
                      name="Inbox"
                      trailing={
                        unreadCount > 0 ? (
                          <span className="rounded-full bg-[var(--forest)] px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white">
                            {unreadCount}
                          </span>
                        ) : undefined
                      }
                      onClick={() => openView("inbox")}
                    />
                  </div>
                </div>
              </aside>

              {/* ── main pane ── */}
              <div className="ld-demo-main">
                {detailIssue ? (
                  /* ── task detail: real IssueHeader + IssueComments, static rail ── */
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
                        onTitleSave={async (title) =>
                          updateIssue(detailIssue.id, { title })
                        }
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
                              [detailIssue.id]: [
                                ...(prev[detailIssue.id] ?? []),
                                comment,
                              ],
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
                          onChange={(status) =>
                            updateIssue(detailIssue.id, { status })
                          }
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
                        <PropertyRow label="Project">
                          {detailIssue.project.name}
                        </PropertyRow>
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
                ) : view === "tasks" ? (
                  <div className="min-h-0 flex-1 overflow-y-auto p-5 sm:p-6">
                    <PanelMasthead
                      overline={DEMO_COMPANY_NAME}
                      title="Tasks"
                      meta={`${issues.length} tasks`}
                    />
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
                        onNewTask={() => {}}
                      />
                    </div>
                  </div>
                ) : view === "inbox" ? (
                  <div className="min-h-0 flex-1 overflow-y-auto p-5 sm:p-6">
                    <PanelMasthead
                      overline={DEMO_COMPANY_NAME}
                      title="Inbox"
                      meta={unreadCount > 0 ? `${unreadCount} unread` : undefined}
                    />
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
                ) : (
                  /* ── chat: real MessageList + lookalike composer ── */
                  <div className="flex min-h-0 flex-1 flex-col">
                    <div className="flex items-center gap-2.5 border-b border-[var(--edge)] px-5 py-3">
                      <Identity name={selectedAgent.name} size="sm" />
                      <span className="text-xs text-[var(--mute)]">
                        {selectedAgent.title}
                      </span>
                      {selectedAgent.status === "running" && <LiveRunDot />}
                    </div>
                    {selectedAgent.id === FRIDAY_ID ? (
                      <MessageList
                        renderGroups={DEMO_RENDER_GROUPS}
                        agentName={selectedAgent.name}
                      />
                    ) : (
                      <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-[var(--mute)]">
                        {selectedAgent.name} is {selectedAgent.status ?? "idle"} —
                        nothing in this chat yet.
                      </div>
                    )}
                    {/* composer lookalike — the real ChatInput needs the
                        gateway; here, sending routes into signup. */}
                    <form
                      className="flex items-center gap-2 border-t border-[var(--edge)] p-3"
                      onSubmit={(e) => {
                        e.preventDefault();
                        router.push("/sign-up");
                      }}
                    >
                      <input
                        value={composerDraft}
                        onChange={(e) => setComposerDraft(e.target.value)}
                        placeholder={`Reply to ${selectedAgent.name}…`}
                        aria-label={`Reply to ${selectedAgent.name}`}
                        className="min-w-0 flex-1 rounded-full border border-[var(--edge)] bg-white px-4 py-2.5 text-sm text-[var(--ink)] placeholder:text-[var(--mute)] focus:border-[var(--forest)] focus:outline-none"
                      />
                      <button
                        type="submit"
                        aria-label="Send"
                        className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[var(--forest)] text-[var(--cream)] transition-colors hover:bg-[#0a5138]"
                      >
                        <Send size={15} />
                      </button>
                    </form>
                  </div>
                )}
              </div>
            </div>
          </div>
        </InlineTeamsNavProvider>
      </div>
    </section>
  );
}
