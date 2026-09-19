import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ImportBackupDialog } from "../ImportBackupDialog";

const mockPost = vi.fn();
const mockDel = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: mockPost, del: mockDel }) }));

// The dialog reads the Teams roster to pick a target agent (same precedent as
// HireManagerDialog); mocking the hook keeps real SWR + the api client out of these tests.
const mockRead = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: mockRead }) }));

// The ONLY preview shape the backend produces. The OpenClaw-backup fields this fixture used to
// also carry (kind, agents, plugins, mcp_servers, file_count, cron_jobs, skipped_big_files) went
// with that rail — the router 400s any archive that is not a ~/.claude slice.
const PREVIEW = {
  import_id: "import-claude456789",
  name: "Claude Code context",
  claude: {
    collections: [
      { id: "isol8-abc123", label: "isol8", file_count: 3 },
      { id: "notes-def456", label: "notes", file_count: 1 },
    ],
    has_user_memory: true,
    skills: ["graphify"],
    skipped: { credentials: 2 },
    safety_flags: [{ pattern: "aws-access-key", severity: "high", file: ".claude/projects/isol8/memory/leak.md", line: 1 }],
  },
};

const ROSTER = { agents: [{ id: "agent1", name: "Friday", role: "ceo" }, { id: "agent2", name: "Scout", role: "general" }] };

function pickFile(size = 1024) {
  const file = new File([new Uint8Array(size)], "claude-code.tar.gz", { type: "application/gzip" });
  fireEvent.change(screen.getByLabelText(/choose a claude code archive/i), { target: { files: [file] } });
}

function wire(overrides: { preview?: unknown; confirm?: unknown } = {}) {
  mockPost.mockImplementation(async (url: string) => {
    if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
    if (url === "/marketplace/import") return overrides.preview ?? PREVIEW;
    if (url.endsWith("/confirm")) {
      if (!overrides.confirm) throw new Error("unexpected confirm");
      return overrides.confirm;
    }
    throw new Error("unexpected " + url);
  });
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

  it("never offers an OpenClaw backup import", () => {
    // The backend refuses any archive that is not a ~/.claude slice, so every OpenClaw-backup
    // affordance this dialog used to render — the title, the `openclaw backup create`
    // instruction, the agent/plugin/MCP checklists — walked the seller into a guaranteed 400.
    const { container } = render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    expect(container.textContent).not.toMatch(/openclaw/i);
    expect(container.textContent).not.toMatch(/backup/i);
    expect(screen.getByText(/import your claude code context/i)).toBeInTheDocument();
  });

  it("presigns, PUTs to S3, previews, and confirms onto the chosen agent", async () => {
    wire({
      confirm: {
        warnings: [],
        claude_items: [
          { item: "memory:isol8-abc123", status: "applied" },
          { item: "user-memory", status: "applied" },
          { item: "skill:graphify", status: "skipped_exists" },
        ],
      },
    });
    const onImported = vi.fn();
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={onImported} />);
    pickFile();
    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith("https://s3/put", expect.objectContaining({ method: "PUT" })));
    expect(await screen.findByText("Claude Code context")).toBeInTheDocument();
    expect(screen.getByText(/2 project memories · 4 memory files · 1 skill/)).toBeInTheDocument();
    expect(screen.getByText(/appended to the agent/)).toBeInTheDocument();
    expect(screen.getByText(/2 credentials/)).toBeInTheDocument();
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
    // per-item results render after confirm; the dialog stays open (the parent closes it inside
    // onImported) until Done is clicked
    expect(await screen.findByText(/skill:graphify — already exists/)).toBeInTheDocument();
    expect(screen.getByText(/user-memory — applied/)).toBeInTheDocument();
    expect(onImported).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /done/i }));
    expect(onImported).toHaveBeenCalledWith("agent1");
  });

  it("renders confirm warnings on the results panel", async () => {
    wire({
      confirm: { warnings: ["skipped 1 file over the size cap"], claude_items: [{ item: "user-memory", status: "applied" }] },
    });
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    pickFile();
    await screen.findByText("Claude Code context");
    fireEvent.change(screen.getByLabelText(/target agent/i), { target: { value: "agent1" } });
    fireEvent.click(screen.getByRole("button", { name: /import context/i }));
    expect(await screen.findByText(/over the size cap/)).toBeInTheDocument();
  });

  it("disables confirm when nothing would land", async () => {
    wire({ preview: { ...PREVIEW, claude: { ...PREVIEW.claude, has_user_memory: false } } });
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    pickFile();
    await screen.findByText("Claude Code context");
    fireEvent.change(screen.getByLabelText(/target agent/i), { target: { value: "agent1" } });
    const confirmBtn = screen.getByRole("button", { name: /import context/i });
    expect(confirmBtn).toBeEnabled();
    // uncheck both collections and the one skill → an empty confirm would only delete the
    // staging behind an empty success, so the button must disable
    fireEvent.click(screen.getByLabelText("isol8"));
    fireEvent.click(screen.getByLabelText("notes"));
    fireEvent.click(screen.getByLabelText("graphify"));
    expect(confirmBtn).toBeDisabled();
    fireEvent.click(screen.getByLabelText("graphify"));
    expect(confirmBtn).toBeEnabled();
  });

  it("surfaces backend detail on failure and discards on cancel", async () => {
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") throw Object.assign(new Error("boom"), { body: { detail: "no Claude Code data in this archive" } });
      throw new Error("unexpected " + url);
    });
    const onOpenChange = vi.fn();
    render(<ImportBackupDialog open onOpenChange={onOpenChange} onImported={() => {}} />);
    pickFile();
    expect(await screen.findByText(/no Claude Code data in this archive/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps the staged import open when confirm fails, so the seller can retry", async () => {
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") return PREVIEW;
      if (url.endsWith("/confirm")) throw Object.assign(new Error("boom"), { body: { detail: "not in this import: typo" } });
      throw new Error("unexpected " + url);
    });
    const onOpenChange = vi.fn();
    render(<ImportBackupDialog open onOpenChange={onOpenChange} onImported={() => {}} />);
    pickFile();
    await screen.findByText("Claude Code context");
    fireEvent.change(screen.getByLabelText(/target agent/i), { target: { value: "agent1" } });
    fireEvent.click(screen.getByRole("button", { name: /import context/i }));
    expect(await screen.findByText(/not in this import: typo/)).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /import context/i })).toBeEnabled();
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
    wire();
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
    expect(screen.getByLabelText(/choose a claude code archive/i)).toBeEnabled();
  });

  it("browser: without the Tauri bridge the button does not render", () => {
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    expect(screen.getByLabelText(/choose a claude code archive/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /import from this mac/i })).not.toBeInTheDocument();
  });

  it("does not hit the Teams BFF before a preview exists", () => {
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    expect(mockRead).toHaveBeenCalledWith(null);
  });
});
