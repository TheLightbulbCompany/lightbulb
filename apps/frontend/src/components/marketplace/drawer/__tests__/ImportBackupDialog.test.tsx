import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ImportBackupDialog } from "../ImportBackupDialog";

const mockPost = vi.fn();
const mockDel = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: mockPost, del: mockDel }) }));

// The claude branch reads the Teams roster (same precedent as HireManagerDialog);
// mocking the hook keeps real SWR + the api client out of these tests entirely.
const mockRead = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: mockRead }) }));

const PREVIEW = {
  import_id: "import-abcdef123456", kind: "backup", agent_id: "main", name: "Quill", description: "dry, precise",
  agents: [{ id: "main", name: "Quill", has_history: true, history_bytes: 4096 }, { id: "work", name: "Work Bot", has_history: false, history_bytes: 0 }],
  plugins: [{ id: "weather", spec: "openclaw-weather@1.2.0", has_dependencies: true }],
  file_count: 7, files: ["./AGENTS.md"], mcp_servers: ["gh"], skills: ["remotion", "notes"], cron_jobs: 1,
  safety_flags: [], warnings: ["backup was written by OpenClaw 2026.3.11; Lightbulb runs 2026.7.2 — unsupported config keys are dropped, not failed"],
  skipped_big_files: [],
};

const CLAUDE_PREVIEW = {
  import_id: "import-claude456789", kind: "claude", agent_id: "", name: "Claude Code context", description: "",
  agents: [], plugins: [], file_count: 0, files: [], mcp_servers: [], skills: [], cron_jobs: 0,
  safety_flags: [], warnings: [], skipped_big_files: [],
  claude: {
    collections: [
      { id: "isol8-abc123", label: "isol8", file_count: 3 },
      { id: "notes-def456", label: "notes", file_count: 1 },
    ],
    has_user_memory: true,
    skills: ["graphify"],
    skipped: { credentials: 2 },
    safety_flags: [{ pattern: "aws-access-key", severity: "high", file: ".claude/projects/isol8/memory/leak.md", line: 1, snippet: "" }],
  },
};

const ROSTER = { agents: [{ id: "agent1", name: "Friday", role: "ceo" }, { id: "agent2", name: "Scout", role: "general" }] };

function pickFile(size = 1024) {
  const file = new File([new Uint8Array(size)], "backup.tar.gz", { type: "application/gzip" });
  fireEvent.change(screen.getByLabelText(/choose a backup archive/i), { target: { files: [file] } });
}

describe("ImportBackupDialog", () => {
  const realFetch = global.fetch;
  beforeEach(() => {
    mockPost.mockReset(); mockDel.mockReset(); mockRead.mockReset();
    mockRead.mockReturnValue({ data: ROSTER, isLoading: false, error: undefined });
    global.fetch = vi.fn().mockResolvedValue({ ok: true }) as unknown as typeof fetch;
  });
  afterEach(() => {
    global.fetch = realFetch;
    delete (window as unknown as { __TAURI__?: unknown }).__TAURI__;
  });

  it("presigns, PUTs to S3, previews, confirms the checklist", async () => {
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "imports/u/x.tar.gz", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") return PREVIEW;
      if (url.endsWith("/confirm")) return { agents: [{ source_agent_id: "main", agent_id: "new-uuid", name: "Quill", ok: true, warnings: [] }], plugins_installed: ["weather"], history_staged: ["new-uuid"], restarting: true, warnings: [] };
      throw new Error("unexpected " + url);
    });
    const onImported = vi.fn();
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={onImported} />);
    pickFile();
    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith("https://s3/put", expect.objectContaining({ method: "PUT" })));
    expect(await screen.findByText("Quill")).toBeInTheDocument();
    // backup header + agent-candidate list still render on kind: "backup"
    expect(screen.getByText(/7 files · 1 scheduled jobs/)).toBeInTheDocument();
    expect(screen.getByLabelText("Work Bot")).toBeInTheDocument();
    expect(screen.getByText(/2026\.3\.11/)).toBeInTheDocument();
    // deselect "work", deselect skill "notes", opt into history
    fireEvent.click(screen.getByLabelText("Work Bot"));
    fireEvent.click(screen.getByLabelText("notes"));
    fireEvent.click(screen.getByLabelText(/bring conversation history/i));
    fireEvent.click(screen.getByRole("button", { name: /add to my team/i }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/marketplace/import/import-abcdef123456/confirm", expect.objectContaining({
      agent_ids: ["main"], skills: ["remotion"], mcp_servers: ["gh"], include_memory: true, include_cron: true, plugins: ["weather"], include_history: true,
    })));
    await waitFor(() => expect(onImported).toHaveBeenCalledWith("new-uuid"));
  });

  it("allows importing into a large roster without an Ultra upsell", async () => {
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") return PREVIEW;
      throw new Error("unexpected " + url);
    });
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    pickFile();
    await screen.findByText("Quill");
    expect(screen.queryByText(/Ultra/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add to my team/i })).toBeEnabled();
  });

  it("surfaces backend detail on failure and discards on cancel", async () => {
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") throw Object.assign(new Error("boom"), { body: { detail: "no OpenClaw backup found" } });
      throw new Error("unexpected " + url);
    });
    const onOpenChange = vi.fn();
    render(<ImportBackupDialog open onOpenChange={onOpenChange} onImported={() => {}} />);
    pickFile();
    expect(await screen.findByText(/no OpenClaw backup found/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("shows partial failures and lets the user continue with what succeeded", async () => {
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") return PREVIEW;
      if (url.endsWith("/confirm")) return {
        agents: [
          { source_agent_id: "main", agent_id: "uuid-ok", name: "Quill", ok: true, warnings: [] },
          { source_agent_id: "work", agent_id: null, name: "Work Bot", ok: false, error: "container starting — retry in a moment" },
        ],
        plugins_installed: [], history_staged: [], restarting: false, warnings: [],
      };
      throw new Error("unexpected " + url);
    });
    const onImported = vi.fn();
    const onOpenChange = vi.fn();
    render(<ImportBackupDialog open onOpenChange={onOpenChange} onImported={onImported} />);
    pickFile();
    await screen.findByText("Quill");
    fireEvent.click(screen.getByRole("button", { name: /add to my team/i }));
    expect(await screen.findByText(/Work Bot — container starting/)).toBeInTheDocument();
    // dialog must NOT have closed: no onImported/onOpenChange call yet
    expect(onImported).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /continue with 1 imported/i }));
    expect(onImported).toHaveBeenCalledWith("uuid-ok");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("non-claude confirm: response warnings render alongside a partial failure", async () => {
    // Backend-honest AgentCore skip (hermes/honest-agentcore-branches) surfaced in
    // ImportConfirmResponse.warnings — previously only captured on the claude branch.
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") return PREVIEW;
      if (url.endsWith("/confirm")) return {
        agents: [
          { source_agent_id: "main", agent_id: "uuid-ok", name: "Quill", ok: true, warnings: [] },
          { source_agent_id: "work", agent_id: null, name: "Work Bot", ok: false, error: "container starting — retry in a moment" },
        ],
        plugins_installed: [], history_staged: [], restarting: false,
        warnings: ["Installing plugins is unsupported_on_agentcore — skipped."],
      };
      throw new Error("unexpected " + url);
    });
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    pickFile();
    await screen.findByText("Quill");
    fireEvent.click(screen.getByRole("button", { name: /add to my team/i }));
    expect(await screen.findByText(/Work Bot — container starting/)).toBeInTheDocument();
    expect(screen.getByText(/unsupported_on_agentcore — skipped/)).toBeInTheDocument();
  });

  it("claude preview: agent picker gates confirm, collection toggles ride the POST body", async () => {
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") return CLAUDE_PREVIEW;
      if (url.endsWith("/confirm")) return {
        agents: [], plugins_installed: [], history_staged: [], restarting: true, warnings: [],
        claude_items: [
          { item: "memory:isol8-abc123", status: "applied" },
          { item: "user-memory", status: "applied" },
          { item: "skill:graphify", status: "skipped_exists" },
        ],
      };
      throw new Error("unexpected " + url);
    });
    const onImported = vi.fn();
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={onImported} />);
    pickFile();
    expect(await screen.findByText("Claude Code context")).toBeInTheDocument();
    // the backup header line must NOT render on the claude branch
    expect(screen.queryByText(/scheduled jobs/)).not.toBeInTheDocument();
    // claude-specific summary + static lines
    expect(screen.getByText(/2 project memories · 4 memory files · 1 skill/)).toBeInTheDocument();
    expect(screen.getByText(/appended to the agent/)).toBeInTheDocument();
    expect(screen.getByText(/aws-access-key/)).toBeInTheDocument();
    // confirm is disabled until a target agent is chosen
    const confirmBtn = screen.getByRole("button", { name: /import context/i });
    expect(confirmBtn).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/target agent/i), { target: { value: "agent1" } });
    expect(confirmBtn).toBeEnabled();
    // unchecking a collection removes its id from the confirm body
    fireEvent.click(screen.getByLabelText("notes"));
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/marketplace/import/import-claude456789/confirm", {
      target_agent_id: "agent1", claude_collections: ["isol8-abc123"], claude_skills: ["graphify"],
    }));
    // per-item results rendered after confirm; the dialog stays open (the
    // parent closes it inside onImported) until Done is clicked
    expect(await screen.findByText(/skill:graphify — already exists/)).toBeInTheDocument();
    expect(screen.getByText(/user-memory — applied/)).toBeInTheDocument();
    expect(onImported).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /done/i }));
    expect(onImported).toHaveBeenCalledWith("agent1");
  });

  it("claude confirm: response warnings render on the results panel", async () => {
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") return CLAUDE_PREVIEW;
      if (url.endsWith("/confirm")) return {
        agents: [], plugins_installed: [], history_staged: [], restarting: false,
        warnings: ["workspace restart could not be started — imported memory/skills will apply on its next restart"],
        claude_items: [{ item: "user-memory", status: "applied" }],
      };
      throw new Error("unexpected " + url);
    });
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    pickFile();
    await screen.findByText("Claude Code context");
    fireEvent.change(screen.getByLabelText(/target agent/i), { target: { value: "agent1" } });
    fireEvent.click(screen.getByRole("button", { name: /import context/i }));
    expect(await screen.findByText(/restart could not be started/)).toBeInTheDocument();
  });

  it("claude confirm: disabled when nothing is selected and there is no user memory", async () => {
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") return { ...CLAUDE_PREVIEW, claude: { ...CLAUDE_PREVIEW.claude, has_user_memory: false } };
      throw new Error("unexpected " + url);
    });
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    pickFile();
    await screen.findByText("Claude Code context");
    fireEvent.change(screen.getByLabelText(/target agent/i), { target: { value: "agent1" } });
    const confirmBtn = screen.getByRole("button", { name: /import context/i });
    expect(confirmBtn).toBeEnabled();
    // uncheck both collections and the one skill → an empty confirm would only
    // delete the staging, so the button must disable
    fireEvent.click(screen.getByLabelText("isol8"));
    fireEvent.click(screen.getByLabelText("notes"));
    fireEvent.click(screen.getByLabelText("graphify"));
    expect(confirmBtn).toBeDisabled();
    fireEvent.click(screen.getByLabelText("graphify"));
    expect(confirmBtn).toBeEnabled();
  });

  it("desktop: a native export resolving after the dialog closed is dropped", async () => {
    let resolveExport!: (v: unknown) => void;
    const invoke = vi.fn().mockReturnValue(new Promise((r) => { resolveExport = r; }));
    (window as unknown as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke } };
    const onOpenChange = vi.fn();
    render(<ImportBackupDialog open onOpenChange={onOpenChange} onImported={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: /import from this mac/i }));
    // close the dialog (Escape — the Cancel button is disabled while busy)
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
    resolveExport({ base64: btoa("gzip-bytes"), file_count: 1, total_bytes: 10 });
    await new Promise((r) => setTimeout(r, 0));
    // the resolved file must not feed handleFile: no presign, no upload
    expect(mockPost).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("desktop: Import from this Mac rides the same presign→PUT→scan path as the file input", async () => {
    const invoke = vi.fn().mockResolvedValue({ base64: btoa("gzip-bytes"), file_count: 3, total_bytes: 42 });
    (window as unknown as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke } };
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") return CLAUDE_PREVIEW;
      throw new Error("unexpected " + url);
    });
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: /import from this mac/i }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/marketplace/import/presign", expect.objectContaining({ filename: "claude-code.tar.gz" })));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith("https://s3/put", expect.objectContaining({ method: "PUT" })));
    expect(invoke).toHaveBeenCalledWith("export_claude_context");
    expect(await screen.findByText("Claude Code context")).toBeInTheDocument();
  });

  it("desktop: a rejected invoke renders the Rust error message verbatim", async () => {
    const invoke = vi.fn().mockRejectedValue("No Claude Code data found at ~/.claude on this Mac.");
    (window as unknown as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke } };
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: /import from this mac/i }));
    expect(await screen.findByText("No Claude Code data found at ~/.claude on this Mac.")).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
    // back on the pick phase — the fallback file input is usable again
    expect(screen.getByLabelText(/choose a backup archive/i)).toBeEnabled();
  });

  it("browser: without the Tauri bridge the button does not render", () => {
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    expect(screen.getByLabelText(/choose a backup archive/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /import from this mac/i })).not.toBeInTheDocument();
  });

  it("renders safety flags in the preview", async () => {
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") return {
        ...PREVIEW,
        safety_flags: [{ pattern: "hardcoded-secret", severity: "high", file: "IDENTITY.md", line: 3, snippet: "" }],
      };
      throw new Error("unexpected " + url);
    });
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    pickFile();
    await screen.findByText("Quill");
    expect(screen.getByText(/hardcoded-secret/)).toBeInTheDocument();
    expect(screen.getByText(/IDENTITY\.md/)).toBeInTheDocument();
  });
});
