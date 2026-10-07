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
import { PROVIDER_BRAND } from "./providerBrand";

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
  // ComponentType, not ElementType — ElementType includes intrinsic tag names, and
  // @react-three/fiber's JSX augmentation makes <Icon size> intersect to never across them.
  icon: React.ComponentType<{ size?: number; className?: string }>;
  brandColor: string;
}

const PROVIDER_CONFIG: Record<string, ProviderConfig> = {
  ai21: { displayName: "AI21", icon: Ai21Icon, brandColor: PROVIDER_BRAND.ai21 },
  amazon: { displayName: "Amazon", icon: AwsIcon, brandColor: PROVIDER_BRAND.amazon },
  anthropic: { displayName: "Anthropic", icon: AnthropicIcon, brandColor: PROVIDER_BRAND.anthropic },
  cohere: { displayName: "Cohere", icon: CohereIcon, brandColor: PROVIDER_BRAND.cohere },
  deepseek: { displayName: "DeepSeek", icon: DeepSeekIcon, brandColor: PROVIDER_BRAND.deepseek },
  "glm-4": { displayName: "Zhipu AI", icon: ZhipuIcon, brandColor: PROVIDER_BRAND.zhipu },
  google: { displayName: "Google", icon: GoogleIcon, brandColor: PROVIDER_BRAND.google },
  meta: { displayName: "Meta", icon: MetaIcon, brandColor: PROVIDER_BRAND.meta },
  minimax: { displayName: "Minimax", icon: MinimaxIcon, brandColor: PROVIDER_BRAND.minimax },
  mistral: { displayName: "Mistral", icon: MistralIcon, brandColor: PROVIDER_BRAND.mistral },
  moonshot: { displayName: "Moonshot", icon: MoonshotIcon, brandColor: PROVIDER_BRAND.moonshot },
  moonshotai: { displayName: "Moonshot", icon: MoonshotIcon, brandColor: PROVIDER_BRAND.moonshot },
  nvidia: { displayName: "Nvidia", icon: NvidiaIcon, brandColor: PROVIDER_BRAND.nvidia },
  openai: { displayName: "OpenAI", icon: OpenAIIcon, brandColor: PROVIDER_BRAND.openai },
  qwen: { displayName: "Qwen", icon: QwenIcon, brandColor: PROVIDER_BRAND.qwen },
  zhipu: { displayName: "Zhipu AI", icon: ZhipuIcon, brandColor: PROVIDER_BRAND.zhipu },
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
    brandColor: "var(--midnight-muted)",
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
          className="gap-2 h-9 px-3 bg-cream border-[var(--edge)] text-[var(--midnight)] hover:bg-[var(--cream-deep)] hover:border-[var(--midnight-faint)]"
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
        className="!w-[320px] !p-0 rounded-xl border-[var(--edge)] bg-cream text-[var(--midnight)] shadow-md"
        style={{ maxHeight: 320 }}
      >
        {/* Search header */}
        <div className="p-3 border-b border-[var(--edge-soft)]">
          <div className="relative">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-[var(--midnight-faint)]" />
            <Input
              placeholder="Search models..."
              className="pl-9 bg-cream-deep border-[var(--edge-soft)] text-[var(--midnight)] placeholder:text-[var(--midnight-muted)] h-9"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        </div>

        {/* Scrollable provider groups */}
        <div
          className="overflow-y-auto overscroll-contain [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar-thumb]:bg-[var(--edge)] [&::-webkit-scrollbar-thumb]:rounded-full"
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
              <div className="px-4 py-8 text-center text-sm text-[var(--midnight-muted)]">
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
                      className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-[var(--cream-deep)] transition-colors"
                    >
                      <ChevronRight
                        className={cn(
                          "h-3 w-3 text-[var(--midnight-faint)] transition-transform duration-(--dur-base)",
                          isExpanded && "rotate-90"
                        )}
                      />
                      <ProviderIcon config={group.config} size={16} />
                      <span className="font-medium text-[var(--midnight)]">
                        {group.config.displayName}
                      </span>
                      <span className="ml-auto text-caption text-[var(--midnight-muted)] bg-[var(--cream-deep)] px-1.5 py-0.5 rounded-full">
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
                                  ? "bg-[var(--cream-deeper)] text-[var(--midnight)]"
                                  : "text-[var(--midnight-soft)] hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
                              )}
                            >
                              <div className="flex items-center gap-2 min-w-0">
                                <span className="font-medium truncate text-left">
                                  {model.name}
                                </span>
                                {tierModel && isTierModel(model.id, tierModel) && (
                                  <span className="shrink-0 text-2xs font-medium px-1.5 py-0.5 rounded-full bg-[var(--midnight)]/10 text-[var(--midnight)] border border-[var(--midnight)]/15">
                                    Included
                                  </span>
                                )}
                              </div>
                              {isSelected && (
                                <Check className="h-3 w-3 text-[var(--midnight)] shrink-0 ml-2" />
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

      </PopoverContent>
    </Popover>
  );
}
