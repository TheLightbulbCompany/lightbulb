"use client";

import { useState } from "react";
import Link from "next/link";

import { AgentDetailPanel } from "@/components/chat/AgentDetailPanel";
import { GalleryItemRow } from "@/components/chat/GalleryItemRow";
import { useAgents } from "@/hooks/useAgents";
import { useCatalog, type CatalogAgent, type DeployResult } from "@/hooks/useCatalog";
import { capture } from "@/lib/analytics";

interface GallerySectionProps {
  onAgentDeployed?: (result: DeployResult) => void;
}

export function GallerySection({ onAgentDeployed }: GallerySectionProps) {
  const { agents, isLoading, deploy } = useCatalog();
  const { refresh: refreshAgents } = useAgents();
  const [selected, setSelected] = useState<CatalogAgent | null>(null);

  if (isLoading) return null;
  if (agents.length === 0) return null;

  const handleDeploy = async (slug: string) => {
    const result = await deploy(slug);
    capture("catalog_agent_deployed", {
      slug: result.slug,
      version: result.version,
    });
    await refreshAgents();
    onAgentDeployed?.(result);
    return result;
  };

  return (
    <>
      <div className="mt-4 border-t border-[#e0dbd0] pt-3">
        <div className="px-2 mb-2 flex items-baseline justify-between">
          <h3 className="font-editorial-serif text-sm italic text-[#1a1a1a]">Gallery</h3>
          <Link href="/marketplace" className="text-[10px] font-bold uppercase tracking-wide text-[#06402B] no-underline">
            Browse all →
          </Link>
        </div>
        <p className="px-2 mb-2 text-[11px] leading-4 text-[#8a8578]">
          One-click hires from the marketplace.
        </p>
        <div className="space-y-0.5">
          {agents.map((a) => (
            <GalleryItemRow
              key={a.slug}
              agent={a}
              onDeploy={handleDeploy}
              onOpenInfo={setSelected}
            />
          ))}
        </div>
      </div>
      <AgentDetailPanel
        agent={selected}
        onClose={() => setSelected(null)}
        onDeploy={handleDeploy}
      />
    </>
  );
}
