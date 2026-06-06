"use client";

import { useState } from "react";

import { Wrench, Server } from "lucide-react";
import { cn } from "@/lib/utils";
import { SkillsTab } from "./SkillsTab";
import { McpServersTab } from "./McpServersTab";

type SkillsPanelTab = "skills" | "mcp";

const TABS: { id: SkillsPanelTab; label: string; icon: typeof Wrench }[] = [
  { id: "skills", label: "Skills", icon: Wrench },
  { id: "mcp", label: "MCP Servers", icon: Server },
];

/**
 * Teams-surface skill/MCP container. The control dashboard uses
 * CapabilitiesPanel instead; this file is kept behaviorally identical
 * because TeamsPanelRouter lazy-imports SkillsPanel. Both share SkillsTab.
 */
export function SkillsPanel({ agentId }: { agentId?: string }) {
  const [activeTab, setActiveTab] = useState<SkillsPanelTab>("skills");

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
        {activeTab === "skills" && <SkillsTab agentId={agentId} />}
        {activeTab === "mcp" && <McpServersTab agentId={agentId} />}
      </div>
    </div>
  );
}
