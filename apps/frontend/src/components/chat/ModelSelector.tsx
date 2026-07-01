"use client";

import * as React from "react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Check, ChevronDown, ChevronRight, Search, Cpu } from "lucide-react";
import {
  Ai21Icon,
  AnthropicIcon,
  AwsIcon,
  CohereIcon,
  DeepSeekIcon,
  GoogleIcon,
  MetaIcon,
  MinimaxIcon,
  MistralIcon,
  MoonshotIcon,
  NvidiaIcon,
  OpenAIIcon,
  QwenIcon,
  ZhipuIcon,
} from "./ProviderIcons";
import { cn } from "@/lib/utils";

interface Model {
  id: string;
  name: string;
}

interface ModelSelectorProps {
  models: Model[];
  selectedModel: string;
  onModelChange: (modelId: string) => void;
  disabled?: boolean;
  /** Model ID that is included with the user's current tier (shown with badge). */
  tierModel?: string;
}

interface ProviderConfig {
  displayName: string;
  icon: React.ElementType;
  brandColor: string;
}

const PROVIDER_CONFIG: Record<string, ProviderConfig> = {
  ai21: { displayName: "AI21", icon: Ai21Icon, brandColor: "#6336E7" },
  amazon: { displayName: "Amazon", icon: AwsIcon, brandColor: "#FF9900" },
  anthropic: { displayName: "Anthropic", icon: AnthropicIcon, brandColor: "#D4A574" },
  cohere: { displayName: "Cohere", icon: CohereIcon, brandColor: "#39594D" },
  deepseek: { displayName: "DeepSeek", icon: DeepSeekIcon, brandColor: "#4D6BFE" },
  "glm-4": { displayName: "Zhipu AI", icon: ZhipuIcon, brandColor: "#3859FF" },
  google: { displayName: "Google", icon: GoogleIcon, brandColor: "#4285F4" },
  meta: { displayName: "Meta", icon: MetaIcon, brandColor: "#0082FB" },
  minimax: { displayName: "Minimax", icon: MinimaxIcon, brandColor: "#F23F5D" },
  mistral: { displayName: "Mistral", icon: MistralIcon, brandColor: "#FA520F" },
  moonshot: { displayName: "Moonshot", icon: MoonshotIcon, brandColor: "#8B8BF5" },
  moonshotai: { displayName: "Moonshot", icon: MoonshotIcon, brandColor: "#8B8BF5" },
  nvidia: { displayName: "Nvidia", icon: NvidiaIcon, brandColor: "#74B71B" },
  openai: { displayName: "OpenAI", icon: OpenAIIcon, brandColor: "#10A37F" },
  qwen: { displayName: "Qwen", icon: QwenIcon, brandColor: "#615CED" },
  zhipu: { displayName: "Zhipu AI", icon: ZhipuIcon, brandColor: "#3859FF" },
};

function extractProvider(modelId: string): string {
  // Bedrock format: "us.anthropic.claude-..." or "anthropic.claude-..."
  const parts = modelId.split(".");
  if (parts.length >= 2) {
    // Skip region prefix like "us" — take the first non-region segment
    const candidate = parts[0].toLowerCase();
    if (candidate.length <= 3 && parts.length >= 3) {
      return parts[1].toLowerCase();
    }
    return candidate;
  }
  // Fallback: use the whole ID up to the first dash or colon
  const match = modelId.match(/^([a-zA-Z0-9]+)/);
  return match ? match[1].toLowerCase() : "unknown";
}

function getProviderConfig(providerId: string): ProviderConfig {
  if (PROVIDER_CONFIG[providerId]) {
    return PROVIDER_CONFIG[providerId];
  }
  return {
    displayName: providerId.charAt(0).toUpperCase() + providerId.slice(1),
    icon: Cpu,
    brandColor: "#888888",
  };
}

interface ProviderGroup {
  providerId: string;
  config: ProviderConfig;
  models: Model[];
}

function ProviderIcon({ config, size = 16 }: { config: ProviderConfig; size?: number }) {
  const Icon = config.icon;
  if (Icon === Cpu) {
    return <Cpu style={{ width: size, height: size, color: config.brandColor }} />;
  }
  return <Icon size={size} />;
}

/**
 * Check if a model ID matches the tier model, handling the `amazon-bedrock/` prefix
 * that OpenClaw adds to Bedrock model IDs in the config catalog.
 */
function isTierModel(modelId: string, tierModelId: string): boolean {
  const bare = modelId.replace(/^amazon-bedrock\//, "");
  const bareTier = tierModelId.replace(/^amazon-bedrock\//, "");
  return bare === bareTier;
}

export function ModelSelector({
  models,
  selectedModel,
  onModelChange,
  disabled = false,
  tierModel,
}: ModelSelectorProps) {
  const [open, setOpen] = React.useState(false);
  const [search, setSearch] = React.useState("");
  const [expandedProviders, setExpandedProviders] = React.useState<Set<string>>(new Set());

  const selectedModelData = models.find((m) => m.id === selectedModel);
  const selectedModelName = selectedModelData?.name || "Select Model";
  const selectedProviderId = selectedModelData ? extractProvider(selectedModelData.id) : null;
  const selectedProviderConfig = selectedProviderId ? getProviderConfig(selectedProviderId) : null;

  // On open: expand group containing selected model, reset search
  React.useEffect(() => {
    if (open) {
      setSearch("");
      if (selectedProviderId) {
        setExpandedProviders(new Set([selectedProviderId]));
      } else {
        setExpandedProviders(new Set());
      }
    }
  }, [open, selectedProviderId]);

  // On search: expand all groups so filtered results are visible
  React.useEffect(() => {
    if (search) {
      const allProviderIds = new Set(models.map((m) => extractProvider(m.id)));
      setExpandedProviders(allProviderIds);
    }
  }, [search, models]);

  const groups = React.useMemo((): ProviderGroup[] => {
    const searchLower = search.toLowerCase();
    const filtered = search
      ? models.filter(
          (m) =>
            m.name.toLowerCase().includes(searchLower) ||
            m.id.toLowerCase().includes(searchLower)
        )
      : models;

    const groupMap = new Map<string, Model[]>();
    for (const model of filtered) {
      const providerId = extractProvider(model.id);
      if (!groupMap.has(providerId)) {
        groupMap.set(providerId, []);
      }
      groupMap.get(providerId)!.push(model);
    }

    return Array.from(groupMap.entries())
      .map(([providerId, providerModels]) => ({
        providerId,
        config: getProviderConfig(providerId),
        models: providerModels.sort((a, b) => a.name.localeCompare(b.name)),
      }))
      .sort((a, b) => a.config.displayName.localeCompare(b.config.displayName));
  }, [models, search]);

  const toggleProvider = (providerId: string) => {
    setExpandedProviders((prev) => {
      const next = new Set(prev);
      if (next.has(providerId)) {
        next.delete(providerId);
      } else {
        next.add(providerId);
      }
      return next;
    });
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild disabled={disabled}>
        <Button
          variant="outline"
          size="sm"
          className="gap-2 h-9 px-3 bg-white border-[#e0dbd0] text-[#1a1a1a] hover:bg-[#f3efe6] hover:border-[#c5bfb6] transition-all"
        >
          <span className="flex items-center gap-2">
            <ProviderIcon config={selectedProviderConfig || getProviderConfig("unknown")} size={16} />
            {selectedModelName}
          </span>
          <ChevronDown className="h-3 w-3 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        side="bottom"
        sideOffset={8}
        collisionPadding={16}
        className="!w-[320px] !p-0 rounded-xl border-[#e0dbd0] bg-white text-[#1a1a1a] shadow-[0_2px_8px_rgba(26,26,26,0.04),0_12px_32px_rgba(26,26,26,0.10)]"
        style={{ maxHeight: 320 }}
      >
        {/* Search header */}
        <div className="p-3 border-b border-[#f0ece3]">
          <div className="relative">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-[#b5ae9e]" />
            <Input
              placeholder="Search models..."
              className="pl-9 bg-[#f7f3ea] border-[#e8e3d9] text-[#1a1a1a] placeholder:text-[#b5ae9e] h-9"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        </div>

        {/* Scrollable provider groups */}
        <div
          className="overflow-y-auto overscroll-contain [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar-thumb]:bg-[#e0dbd0] [&::-webkit-scrollbar-thumb]:rounded-full"
          style={{ maxHeight: 240 }}
          onWheel={(e) => {
            const el = e.currentTarget;
            const atTop = el.scrollTop === 0 && e.deltaY < 0;
            const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight && e.deltaY > 0;
            if (!atTop && !atBottom) {
              e.stopPropagation();
            }
          }}
        >
            <div className="py-1">
            {groups.length === 0 ? (
              <div className="px-4 py-8 text-center text-sm text-[#b5ae9e]">
                No models found
              </div>
            ) : (
              groups.map((group) => {
                const isExpanded = expandedProviders.has(group.providerId);

                return (
                  <div key={group.providerId}>
                    {/* Group header */}
                    <button
                      onClick={() => toggleProvider(group.providerId)}
                      className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-[#f7f3ea] transition-colors"
                    >
                      <ChevronRight
                        className={cn(
                          "h-3.5 w-3.5 text-[#b5ae9e] transition-transform duration-200",
                          isExpanded && "rotate-90"
                        )}
                      />
                      <ProviderIcon config={group.config} size={16} />
                      <span className="font-medium text-[#1a1a1a]">
                        {group.config.displayName}
                      </span>
                      <span className="ml-auto text-[11px] text-[#8a8578] bg-[#f3efe6] px-1.5 py-0.5 rounded-full">
                        {group.models.length}
                      </span>
                    </button>

                    {/* Model rows */}
                    {isExpanded && (
                      <div className="pb-1">
                        {group.models.map((model) => {
                          const isSelected = selectedModel === model.id;
                          return (
                            <button
                              key={model.id}
                              title={model.id}
                              onClick={() => {
                                onModelChange(model.id);
                                setOpen(false);
                              }}
                              className={cn(
                                "w-full flex items-center justify-between pl-9 pr-3 py-1.5 text-sm transition-colors group",
                                isSelected
                                  ? "bg-[#f3efe6] text-[#1a1a1a]"
                                  : "text-[#5a5549] hover:bg-[#f7f3ea] hover:text-[#1a1a1a]"
                              )}
                            >
                              <div className="flex items-center gap-2 min-w-0">
                                <span className="font-medium truncate text-left">
                                  {model.name}
                                </span>
                                {tierModel && isTierModel(model.id, tierModel) && (
                                  <span className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-[#06402B]/10 text-[#06402B] border border-[#06402B]/15">
                                    Included
                                  </span>
                                )}
                              </div>
                              {isSelected && (
                                <Check className="h-3.5 w-3.5 text-[#06402B] shrink-0 ml-2" />
                              )}
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })
            )}
            </div>
        </div>

        {/* Footer */}
        <div className="p-2 border-t border-[#f0ece3] bg-[#faf7f2] rounded-b-xl shrink-0">
          <div className="text-[10px] text-center text-[#b5ae9e]">
            Powered by AWS Bedrock
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
