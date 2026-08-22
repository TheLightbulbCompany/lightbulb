import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import { ImportBackupDialog } from "../ImportBackupDialog";

const mockPost = vi.fn();
const mockDel = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: mockPost, del: mockDel }) }));

const PREVIEW = {
  import_id: "import-abcdef123456", kind: "backup", agent_id: "main", name: "Quill", description: "dry, precise",
  agents: [{ id: "main", name: "Quill", has_history: true, history_bytes: 4096 }, { id: "work", name: "Work Bot", has_history: false, history_bytes: 0 }],
  plugins: [{ id: "weather", spec: "openclaw-weather@1.2.0", has_dependencies: true }],
  file_count: 7, files: ["./AGENTS.md"], mcp_servers: ["gh"], skills: ["remotion", "notes"], cron_jobs: 1,
  safety_flags: [], warnings: ["backup was written by OpenClaw 2026.3.11; Isol8 runs 2026.7.2 — unsupported config keys are dropped, not failed"],
  skipped_big_files: [], existing_agents: 2, standard_max_agents: 10, plan: "flat",
};

function pickFile(size = 1024) {
  const file = new File([new Uint8Array(size)], "backup.tar.gz", { type: "application/gzip" });
  fireEvent.change(screen.getByLabelText(/choose a backup archive/i), { target: { files: [file] } });
}

describe("ImportBackupDialog", () => {
  const realFetch = global.fetch;
  beforeEach(() => {
    mockPost.mockReset(); mockDel.mockReset();
    global.fetch = vi.fn().mockResolvedValue({ ok: true }) as unknown as typeof fetch;
  });
  afterEach(() => { global.fetch = realFetch; });

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

  it("shows the Ultra upsell and disables confirm when the sum exceeds the standard cap", async () => {
    mockPost.mockImplementation(async (url: string) => {
      if (url === "/marketplace/import/presign") return { upload_url: "https://s3/put", key: "k", max_bytes: 1e9, expires_in: 900 };
      if (url === "/marketplace/import") return { ...PREVIEW, existing_agents: 9 };
      throw new Error("unexpected " + url);
    });
    render(<ImportBackupDialog open onOpenChange={() => {}} onImported={() => {}} />);
    pickFile();
    await screen.findByText("Quill");
    expect(screen.getByText(/Ultra Pro/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add to my team/i })).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Work Bot")); // 9 + 1 = 10 → OK
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
});
