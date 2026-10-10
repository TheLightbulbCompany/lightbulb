"use client";

import { useCallback, useState } from "react";
import { FileContentViewer } from "@/components/chat/FileContentViewer";
import { useConfigFile } from "@/hooks/useWorkspaceFiles";
import { useApi } from "@/lib/api";
import { cn } from "@/lib/utils";

// The inspector exposes the identity files plus the agent's own notes
// (MEMORY.md), not the full CONFIG_ALLOWLIST (TOOLS/HEARTBEAT/AGENTS live elsewhere). No
// IDENTITY.md: on Hermes an agent's one identity document is SOUL.md, and the
// backend refuses an IDENTITY.md save with a 422 (`workspace_files.py`). Both
// are config-tab writes — saveWorkspaceFile(..., "config") creates them on
// first save when they don't exist on disk yet.
const IDENTITY_FILES = ["SOUL.md", "USER.md", "MEMORY.md"] as const;
type IdentityFile = (typeof IDENTITY_FILES)[number];

interface AgentInstructionsTabProps {
  agentId: string;
}

/**
 * Instructions tab (increment 3 of the unified-nav migration).
 *
 * Edits the agent's identity files (SOUL/USER) in place. Reuses the
 * existing config-file read (`useConfigFile`, which synthesizes an empty
 * editable buffer on 404 so an un-seeded file opens blank + savable) and the
 * shared `FileContentViewer` editor — saves flow through the already-existing
 * `saveWorkspaceFile(..., "config")` PUT path. No new backend.
 */
export function AgentInstructionsTab({ agentId }: AgentInstructionsTabProps) {
  const [selectedFile, setSelectedFile] = useState<IdentityFile>("SOUL.md");
  const [editorDirty, setEditorDirty] = useState(false);

  const { file, isLoading, error } = useConfigFile(agentId, selectedFile);
  const api = useApi();

  const handleSave = useCallback(
    async (content: string) => {
      await api.saveWorkspaceFile(agentId, selectedFile, content, "config");
    },
    [api, agentId, selectedFile],
  );

  // Guard against silently dropping unsaved edits when switching files — same
  // confirm-on-dirty contract FileViewer uses. `handleSelectFile` only ever
  // fires from an onClick, so reading `editorDirty` directly is safe.
  const handleSelectFile = useCallback(
    (next: IdentityFile) => {
      if (next === selectedFile) return;
      if (editorDirty && !window.confirm("You have unsaved changes. Discard them?")) {
        return;
      }
      setSelectedFile(next);
    },
    [selectedFile, editorDirty],
  );

  return (
    <div className="flex flex-col h-full py-4">
      <div
        className="flex items-center gap-1 mb-3"
        role="group"
        aria-label="Identity file"
      >
        {IDENTITY_FILES.map((name) => (
          <button
            key={name}
            type="button"
            aria-pressed={selectedFile === name}
            className={cn(
              "px-3 py-1.5 rounded-md text-xs font-medium font-mono transition-colors",
              selectedFile === name
                ? "bg-[var(--cream)] text-[var(--midnight)]"
                : "text-[var(--midnight-muted)] hover:text-[var(--midnight)]",
            )}
            onClick={() => handleSelectFile(name)}
          >
            {name}
          </button>
        ))}
      </div>

      {selectedFile === "MEMORY.md" ? (
        <p className="mb-2 text-xs text-[var(--fg-mute)]">
          Notes this agent saved for itself. Every room sees them.
        </p>
      ) : null}

      <p className="text-caption text-[var(--midnight-muted)] mb-3">
        Changes apply on the agent&apos;s next session.
      </p>

      <div className="flex-1 min-h-0 border border-[var(--edge)] rounded-md overflow-hidden">
        <FileContentViewer
          file={file}
          isLoading={isLoading}
          error={error ?? null}
          onSave={handleSave}
          onDirtyChange={setEditorDirty}
        />
      </div>
    </div>
  );
}
