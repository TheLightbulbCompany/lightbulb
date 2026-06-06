"use client";

import { useState } from "react";

import { Package, Wrench } from "lucide-react";
import { cn } from "@/lib/utils";
import { PluginsTab } from "./PluginsTab";
import { SkillsTab } from "./SkillsTab";

type CapabilitiesTab = "plugins" | "skills";

const TABS: { id: CapabilitiesTab; label: string; icon: typeof Wrench }[] = [
  { id: "plugins", label: "Plugins", icon: Package },
  { id: "skills", label: "Skills", icon: Wrench },
];

/**
 * Control-dashboard "Capabilities" home. Plugins is the parent map (a plugin
 * bundles tools/skills/providers/channels); Skills is the per-skill store.
 * The raw MCP-servers escape hatch lives under Integrations, not here — it's
 * a credential-bearing connection, not a capability.
 */
export function CapabilitiesPanel({ agentId }: { agentId?: string }) {
  const [activeTab, setActiveTab] = useState<CapabilitiesTab>("plugins");

  return (
    <div className="flex flex-col h-full">
      <div className="flex border-b border-[#e0dbd0] px-2">
        {TABS.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              className={cn(
                "flex items-center gap-1.5 px-3 py-2 text-xs font-medium transition-colors border-b-2 -mb-px",
                activeTab === tab.id
                  ? "border-[#2d8a4e] text-[#1a1a1a]"
                  : "border-transparent text-[#8a8578] hover:text-[#1a1a1a]",
              )}
              onClick={() => setActiveTab(tab.id)}
            >
              <Icon className="h-3.5 w-3.5" />
              {tab.label}
            </button>
          );
        })}
      </div>

      <div className="flex-1 overflow-y-auto">
        {activeTab === "plugins" && <PluginsTab />}
        {activeTab === "skills" && <SkillsTab agentId={agentId} />}
      </div>
    </div>
  );
}
