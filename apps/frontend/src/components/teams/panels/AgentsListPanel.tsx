"use client";

// The roster AND the org chart: same `/agents` read, two views. The retired
// `teams-org-chart` key aliases onto this panel (panelRegistry's
// RETIRED_PANEL_ALIASES) and arrives as `view: "chart"`, so an old link opens
// ONE Agents tab on the chart rather than a second tab drawing the same rows.

import { useMemo, useState } from "react";
import { Bot, List, Network } from "lucide-react";
import {
  EditorialButton,
  EditorialDialog,
  PanelDataTable,
  PanelEmptyState,
  PanelMasthead,
  PanelToolbar,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { AgentCreateChooser } from "@/components/agents/AgentCreateChooser";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { OrgChart } from "@/components/teams/org-chart/OrgChart";
import { TeamCards } from "./TeamCards";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import {
  setAgentsViewMode,
  useAgentsViewMode,
  type AgentsViewMode,
} from "@/components/teams/agents/agentsViewMode";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { notifyApiError } from "@/lib/notify";
import { handleShellLinkClick, navigateShell } from "@/lib/shellNav";
import { openAgentDmWithDraft, openChatPanel } from "@/components/workspace/workspaceController";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { agentInspectorUrl } from "@/components/teams/shared/lib/agentInspectorUrl";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";
import type { CompanyAgent } from "@/components/teams/shared/types";

export interface AgentsListPanelProps {
  /** `?view=` (or the retired `teams-org-chart` key), already validated by the
   *  router. Honoured on this mount without touching the stored preference —
   *  the toggle takes the view back, and the preference is what it always was.
   *  Re-delivered rather than remounted when an already-open panel is
   *  re-opened, so it is compared against its previous value below. */
  initialView?: AgentsViewMode | null;
}

/** `messaging.py` `_handle_candidate`: the name in ASCII, lowercased, runs of anything else as "-", 32 at most.
 *  ponytail: no collision suffix (two agents with one name); the room members' handle wins when there is one. */
export function derivedHandle(name: string): string | null {
  const handle = name.normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32);
  return handle && handle !== "lightbulb" ? handle : null;
}

export function AgentsListPanel({ initialView }: AgentsListPanelProps = {}) {
  const { read } = useTeamsApi();
  const { pause, resume, remove } = useAgentMutations();
  const { data, isLoading, error, mutate } = read<{ agents: CompanyAgent[] }>(
    "/agents",
  );
  const { open: openMarketplace } = useMarketplaceDrawer();
  // With the super-agent on, only Lightbulb's DM is a Router room: a specialist's own DM keeps the legacy path
  // (admission.router_room), so no Request, no private lane. A specialist is asked through Lightbulb instead,
  // its @handle drafted there (admission pins it on the trigger for the Router).
  const superAgent = useSuperAgent();
  const directory = useMessagingWorkspaceOptional()?.directoryProps;
  const frontAgentId = superAgent ? directory?.ceoAgentId : undefined;
  // The front agent reads as the sidebar names it ("Lightbulb"), never its roster name ("CEO").
  const nameOf = (agent: CompanyAgent) => agent.id === frontAgentId
    ? directory?.participants.find((p) => p.kind === "agent" && p.id === agent.id)?.displayName || "Lightbulb"
    : agent.name;
  // The directory's handles come from the open room's members only, so from Lightbulb's DM (no specialists in it) Ask
  // drafted nothing (dev walk W7-7): any room that has the agent, else the handle the server derives from its name.
  const handleOf = (agent: CompanyAgent) => [...(directory?.participants ?? []), ...(directory?.rooms ?? []).flatMap((r) => r.participants)]
    .find((p) => p.kind === "agent" && p.id === agent.id && p.mentionHandle)?.mentionHandle ?? derivedHandle(agent.name);
  const message = (agent: CompanyAgent) => {
    if (!frontAgentId || agent.id === frontAgentId) return openChatPanel(agent.id);
    const handle = handleOf(agent);
    openAgentDmWithDraft(frontAgentId, handle ? `@${handle} ` : "");
  };
  const togglePause = async (a: CompanyAgent) => {
    const paused = a.status === "paused";
    try {
      await (paused ? resume(a.id) : pause(a.id));
    } catch (err) {
      notifyApiError(err, paused ? "Couldn't resume this agent" : "Couldn't pause this agent");
    }
  };
  const deleteAgent = async (a: CompanyAgent) => {
    if (!window.confirm(`Delete ${a.name}? This removes the agent and its workspace.`)) return;
    try {
      await remove(a.id);
    } catch (err) {
      notifyApiError(err, "Couldn't delete this agent");
    }
  };
  const [creating, setCreating] = useState(false);
  const [search, setSearch] = useState("");
  const storedView = useAgentsViewMode();
  const [viewDeepLink, setViewDeepLink] = useState<AgentsViewMode | null>(initialView ?? null);
  const view = viewDeepLink ?? storedView;
  // The org chart only exists without the super-agent; a saved or linked chart view never hides Team's search.
  const chartShown = !superAgent && view === "chart";
  // Adjust-state-during-render, not an effect — the same pattern TasksList
  // uses for its `?groupBy` re-delivery: the workspace merges parameters into an
  // open panel instead of remounting it, and a plain sidebar nav clears them.
  const [prevInitialView, setPrevInitialView] = useState(initialView);
  if (initialView !== prevInitialView) {
    setPrevInitialView(initialView);
    setViewDeepLink(initialView ?? null);
  }

  const agents = data?.agents;
  const filtered = useMemo(() => {
    const list = agents ?? [];
    return search
      ? list.filter((a) =>
          (a.name + " " + (a.role ?? "") + " " + (a.origin ?? ""))
            .toLowerCase()
            .includes(search.toLowerCase()),
        )
      : list;
  }, [agents, search]);

  if (error) {
    return (
      <div
        role="alert"
        className="editorial-page p-8 font-editorial-serif text-xl italic text-[var(--err)]"
      >
        We couldn&rsquo;t load your agents.
      </div>
    );
  }

  const allColumns: PanelDataColumn<CompanyAgent>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      cell: (a) => (
        <div className="font-medium text-[var(--midnight)]">{nameOf(a)}</div>
      ),
    },
    {
      key: "role",
      header: "Role",
      cell: (a) => (
        <span className="label-caps text-[var(--midnight-muted)]">
          {a.role}
        </span>
      ),
    },
    {
      key: "origin",
      header: "Origin",
      cell: (a) => (
        <span className="text-caption text-[var(--midnight-soft)]">
          {a.origin ?? "Custom"}
          {a.origin === "Marketplace" && a.marketplace
            ? ` · ${a.marketplace.slug} v${a.marketplace.version}`
            : ""}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (a) => {
        const paused = a.status === "paused";
        return (
          <span className="flex items-center justify-end gap-2">
            {/* Lightbulb (or any agent with the switch off): its DM. A specialist: asked in Lightbulb's DM. */}
            <button
              type="button"
              className="text-ui hover:underline text-[var(--midnight)]"
              aria-label={frontAgentId && a.id !== frontAgentId ? `Ask ${a.name} through Lightbulb` : `Message ${nameOf(a)}`}
              onClick={() => message(a)}
            >
              {frontAgentId && a.id !== frontAgentId ? "Ask" : "Message"}
            </button>
            <button
              className="text-ui hover:underline text-[var(--midnight-soft)]"
              onClick={() => void togglePause(a)}
            >
              {paused ? "Resume" : "Pause"}
            </button>
            <button
              className="text-ui hover:underline text-[var(--err)]"
              onClick={() => void deleteAgent(a)}
            >
              Delete
            </button>
          </span>
        );
      },
    },
    {
      key: "open",
      header: "",
      align: "right",
      cell: (a) => (
        <a
          href={agentInspectorUrl(a.id)}
          onClick={(e) => handleShellLinkClick(e, agentInspectorUrl(a.id))}
          className="text-ui hover:underline"
          style={{ color: "var(--midnight)" }}
        >
          Open →
        </a>
      ),
    },
  ];
  // Super-agent: specialists by name only; Paperclip's roles (CMO, ENGINEER) and origins are plumbing.
  const columns = superAgent ? allColumns.filter((c) => c.key !== "role" && c.key !== "origin") : allColumns;
  const createLabel = superAgent ? "Hire a specialist" : "New agent";

  return (
    <div className="editorial-page flex flex-col gap-4 p-4 sm:p-5 lg:p-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Team"
        title="Team"
        actions={
          <>
            {/* Hiring moved here from the workspace rail: the same marketplace drawer. Super-agent: one action,
                the chooser (hire from the marketplace, or build one). */}
            {!superAgent && <EditorialButton variant="secondary" onClick={() => openMarketplace({ view: "browse" })}>
              Add an AI employee
            </EditorialButton>}
            <EditorialButton variant="primary" onClick={() => setCreating(true)}>
              {createLabel}
            </EditorialButton>
          </>
        }
      />
      <PanelToolbar
        // Chart view keeps the value (so returning to the list restores the
        // search) but does not offer the box: a control that visibly changes
        // nothing is worse than no control.
        searchValue={chartShown ? "" : search}
        onSearchChange={setSearch}
        searchPlaceholder={chartShown ? "" : "Find agent..."}
        searchAriaLabel="Search agents"
        hideSearch={chartShown}
        actions={superAgent ? undefined : (
          <ViewModeToggle
            view={view}
            onChange={(mode) => {
              // An explicit pick beats the deep link and becomes the preference.
              setViewDeepLink(null);
              setAgentsViewMode(mode);
            }}
          />
        )}
      />
      {isLoading ? (
        <div role="status" className="font-editorial-serif text-xl italic text-[var(--midnight-muted)]">
          Loading…
        </div>
      ) : filtered.length === 0 ? (
        <PanelEmptyState
          icon={Bot}
          headline={search ? "No agents match your search." : "No agents yet."}
          supporting={
            search
              ? undefined
              : "Spin up your first agent — assign a role + start delegating work."
          }
          action={{ label: createLabel, onClick: () => setCreating(true) }}
        />
      ) : superAgent ? (
        // Super-agent: a card per specialist (what it's for, its skills, its recent requests, Ask). The org chart
        // was Paperclip's reporting lines; nothing reports to anyone here.
        <TeamCards agents={filtered} frontAgentId={frontAgentId} nameOf={nameOf} handleOf={handleOf} onAsk={message}
          actions={(a) => <>
            <DropdownMenuItem onSelect={() => void togglePause(a)}>{a.status === "paused" ? "Resume" : "Pause"}</DropdownMenuItem>
            <DropdownMenuItem onSelect={() => navigateShell(agentInspectorUrl(a.id))}>Settings</DropdownMenuItem>
            {a.id !== frontAgentId && <DropdownMenuItem variant="destructive" onSelect={() => void deleteAgent(a)}>Delete</DropdownMenuItem>}
          </>} />
      ) : view === "chart" ? (
        // The chart draws the WHOLE roster, never the search-filtered slice.
        // An org chart is a structure, and filtering a structure is not a
        // smaller structure — it is several orphaned roots that misdescribe who
        // reports to whom. Search stays a list affordance, the way grouping and
        // sort are list-only on Tasks.
        <OrgChart agents={agents ?? []} />
      ) : (
        <PanelDataTable<CompanyAgent>
          rows={filtered}
          columns={columns}
          rowKey={(a) => a.id}
        />
      )}

      {creating && (
        <CreateAgentDialog
          title={createLabel}
          agents={agents ?? []}
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            mutate();
          }}
        />
      )}
    </div>
  );
}

/** Roster or org chart. Modelled on TasksList's list/board toggle — same
 *  control shape, same per-browser persistence idiom. */
function ViewModeToggle({
  view,
  onChange,
}: {
  view: AgentsViewMode;
  onChange: (mode: AgentsViewMode) => void;
}) {
  const buttonClass = (active: boolean) =>
    cn(
      "inline-flex h-7 w-7 items-center justify-center transition-colors",
      active
        ? "bg-[var(--midnight)] text-[var(--cream)]"
        : "text-[var(--midnight-muted)] hover:text-[var(--midnight)]",
    );
  return (
    <div className="flex items-center overflow-hidden rounded-md border border-[var(--edge)]">
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className={buttonClass(view === "list")}
            onClick={() => onChange("list")}
            aria-pressed={view === "list"}
            aria-label="List view"
          >
            <List size={16} />
          </button>
        </TooltipTrigger>
        <TooltipContent>List view</TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className={buttonClass(view === "chart")}
            onClick={() => onChange("chart")}
            aria-pressed={view === "chart"}
            aria-label="Org chart view"
          >
            <Network size={16} />
          </button>
        </TooltipTrigger>
        <TooltipContent>Org chart</TooltipContent>
      </Tooltip>
    </div>
  );
}

function CreateAgentDialog({
  title: dialogTitle,
  agents,
  onClose,
  onCreated,
}: {
  title: string;
  /** Existing roster — the manager picker's candidates. A brand-new agent
   *  has no descendants, so every existing agent is an eligible manager. */
  agents: CompanyAgent[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  const { open: openMarketplace } = useMarketplaceDrawer();
  // Chooser first: steer toward hiring a pre-built marketplace agent; the
  // custom-build form is the secondary path.
  const [mode, setMode] = useState<"choose" | "build">("choose");
  const [name, setName] = useState("");
  const [role, setRole] = useState("engineer");
  const [title, setTitle] = useState("");
  const [reportsTo, setReportsTo] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setSubmitting(true);
    setErr(null);
    try {
      // SECURITY: body intentionally contains NO adapterType, NO adapterConfig,
      // NO url, NO authToken. The BFF synthesizes the openclaw_gateway adapter
      // server-side via core.services.paperclip_adapter_config and rejects any
      // smuggled fields with 422 (Pydantic extra="forbid"). Defense in depth:
      // the UI doesn't even render those inputs.
      //
      // Rich hire (A7): title + reportsTo are already accepted by CreateAgentBody
      // end-to-end. Include them ONLY when set — an empty title would write a
      // blank field and an empty reportsTo means "top level" (omit, don't send "").
      // Budget is deliberately NOT surfaced: per-agent budget is gated on a
      // separate cost_events bridge, not the owner's provider path.
      const body: {
        name: string;
        role: string;
        title?: string;
        reportsTo?: string;
      } = { name, role };
      const trimmedTitle = title.trim();
      if (trimmedTitle) body.title = trimmedTitle;
      if (reportsTo) body.reportsTo = reportsTo;
      await post("/agents", body);
      onCreated();
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  }

  if (mode === "choose") {
    return (
      <EditorialDialog
        open
        onOpenChange={(o) => !o && onClose()}
        title={dialogTitle}
      >
        <AgentCreateChooser
          surface="teams"
          onHire={() => {
            onClose();
            openMarketplace({ view: "browse" });
          }}
          onBuild={() => setMode("build")}
        />
      </EditorialDialog>
    );
  }

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={dialogTitle}
    >
      <div className="flex flex-col gap-3">
        <label className="block">
          <span className="text-ui text-[var(--midnight-soft)]">Name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
          />
        </label>
        <label className="block">
          <span className="text-ui text-[var(--midnight-soft)]">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Senior backend engineer"
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
          />
        </label>
        <label className="block">
          <span className="text-ui text-[var(--midnight-soft)]">Role</span>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
          >
            <option value="engineer">Engineer</option>
            <option value="ceo">CEO</option>
            <option value="manager">Manager</option>
          </select>
        </label>
        <label className="block">
          <span className="text-ui text-[var(--midnight-soft)]">Reports to</span>
          <select
            aria-label="Reports to"
            value={reportsTo}
            onChange={(e) => setReportsTo(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
          >
            <option value="">No manager (top level)</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        {err && (
          <div className="text-err text-ui border border-err bg-cream rounded-lg px-3 py-2">
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
            disabled={submitting || !name}
          >
            {submitting ? "Creating…" : "Create"}
          </EditorialButton>
        </div>
      </div>
    </EditorialDialog>
  );
}
