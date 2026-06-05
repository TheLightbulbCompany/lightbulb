"use client";

import {
  LayoutDashboard,
  Bot,
  Sparkles,
  MessageSquare,
  Radio,
  Clock,
  BarChart3,
  Plug,
  Blocks,
  Wallet,
} from "lucide-react";
import { useOrganization } from "@clerk/nextjs";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useApi } from "@/lib/api";
import { cn } from "@/lib/utils";

interface ControlSidebarProps {
  activePanel?: string;
  onPanelChange?: (panel: string) => void;
}

type UserMeResponse = {
  user_id?: string;
  provider_choice?: "chatgpt_oauth" | "byo_key" | "bedrock_claude" | null;
  byo_provider?: "openai" | "anthropic" | null;
};

const NAV_ITEMS = [
  { key: "overview", label: "Overview", icon: LayoutDashboard },
  { key: "agents", label: "Agents", icon: Bot },
  { key: "skills", label: "Skills", icon: Sparkles },
  { key: "sessions", label: "Sessions", icon: MessageSquare },
  { key: "channels", label: "Channels", icon: Radio },
  { key: "cron", label: "Cron Jobs", icon: Clock },
  { key: "usage", label: "Usage", icon: BarChart3 },
  { key: "llm", label: "LLM Provider", icon: Plug },
  { key: "integrations", label: "Integrations", icon: Blocks },
  { key: "credits", label: "Credits", icon: Wallet },
];

// Panels hidden from non-admin org members
const ADMIN_ONLY_PANELS = new Set(["usage"]);

// Panels that only apply when the user is on the Bedrock-provided plan.
// byo_key + chatgpt_oauth users manage billing directly with their provider.
const BEDROCK_ONLY_PANELS = new Set(["credits"]);

export function ControlSidebar({ activePanel, onPanelChange }: ControlSidebarProps) {
  const api = useApi();
  const { membership } = useOrganization();
  const isOrgAdmin = !membership || membership.role === "org:admin";

  const { data: me } = useSWR<UserMeResponse>(
    "/users/me",
    () => api.get("/users/me") as Promise<UserMeResponse>,
  );
  // While loading, treat the user as Bedrock-eligible so the Credits item
  // doesn't flash on resolve. Once provider_choice resolves to a non-Bedrock
  // value, the item disappears.
  const isBedrockUser = me === undefined || me.provider_choice === "bedrock_claude";

  return (
    <ScrollArea className="flex-1 px-3 py-2">
      <div className="space-y-1">
        {NAV_ITEMS.map(({ key, label, icon: Icon }) => {
          if (ADMIN_ONLY_PANELS.has(key) && !isOrgAdmin) return null;
          if (BEDROCK_ONLY_PANELS.has(key) && !isBedrockUser) return null;
          return (
            <Button
              key={key}
              variant="ghost"
              className={cn(
                "w-full justify-start gap-2 font-normal transition-all h-auto py-1.5",
                activePanel === key
                  ? "bg-white text-[#1a1a1a] shadow-sm"
                  : "text-[#8a8578] hover:text-[#1a1a1a] hover:bg-white/60",
              )}
              onClick={() => onPanelChange?.(key)}
            >
              <Icon className="h-4 w-4 flex-shrink-0 opacity-70" />
              <span className="truncate">{label}</span>
            </Button>
          );
        })}
      </div>
    </ScrollArea>
  );
}
