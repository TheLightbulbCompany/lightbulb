"use client";

import { Info, Loader2, Plus } from "lucide-react";
import { useState } from "react";

import type { CatalogAgent } from "@/hooks/useCatalog";
import { Monogram } from "@/components/agents/AgentIdentity";
import { fromCatalogAgent } from "@/lib/agentDesign";

interface GalleryItemRowProps {
  agent: CatalogAgent;
  onDeploy: (slug: string) => Promise<unknown>;
  onOpenInfo: (agent: CatalogAgent) => void;
}

export function GalleryItemRow({ agent, onDeploy, onOpenInfo }: GalleryItemRowProps) {
  const [deploying, setDeploying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const displayAgent = fromCatalogAgent(agent);

  const handleDeploy = async () => {
    setDeploying(true);
    setError(null);
    try {
      await onDeploy(agent.slug);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to deploy agent";
      setError(message);
    } finally {
      setDeploying(false);
    }
  };

  return (
    <div className="px-2 py-1">
      <div className="flex items-center gap-2 rounded-[10px] px-2 py-1.5 transition-colors hover:bg-white/80 hover:shadow-[0_1px_4px_rgba(26,26,26,0.04)]">
        <Monogram agent={displayAgent} size={28} radius={8} />
        <button
          type="button"
          onClick={() => onOpenInfo(agent)}
          className="min-w-0 flex-1 text-left"
        >
          <span className="block truncate text-[13px] font-medium text-[#1a1a1a]">{displayAgent.name}</span>
          <span className="block truncate font-mono text-[9px] uppercase tracking-[0.1em] text-[#8a8578]">
            {displayAgent.role}
          </span>
        </button>
        <button
          type="button"
          aria-label={`Deploy ${agent.name}`}
          onClick={handleDeploy}
          disabled={deploying}
          className="inline-flex h-6 w-6 items-center justify-center rounded-full border border-[#e0dbd0] text-[#06402B] transition-colors hover:bg-[#06402B] hover:text-white disabled:opacity-50"
        >
          {deploying ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
        </button>
        <button
          type="button"
          aria-label={`Info about ${agent.name}`}
          onClick={() => onOpenInfo(agent)}
          className="inline-flex h-6 w-6 items-center justify-center rounded-full text-[#8a8578] transition-colors hover:bg-white/80 hover:text-[#1a1a1a]"
        >
          <Info className="w-4 h-4" />
        </button>
      </div>
      {error && (
        <div
          role="alert"
          className="mt-1 ml-10 mr-2 text-xs text-[#b32424] bg-[#fdecea] border border-[#f5c2c0] rounded px-2 py-1"
        >
          {error}
        </div>
      )}
    </div>
  );
}
