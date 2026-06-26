"use client";

import { useMemo } from "react";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { HIDDEN_COMMANDS, type SlashCommand } from "@/lib/slashCommands";

type CommandsListEntry = {
  name: string;
  description: string;
  category?: string;
  source: "native" | "skill" | "plugin";
  acceptsArgs?: boolean;
  args?: SlashCommand["args"];
  textAliases?: string[];
};
type CommandsListResult = { commands: CommandsListEntry[] };

export function useSlashCommands(agentId: string | null): {
  commands: SlashCommand[];
  isLoading: boolean;
} {
  const { data, isLoading } = useGatewayRpc<CommandsListResult>(
    agentId ? "commands.list" : null,
    agentId ? { agentId, includeArgs: true, scope: "text" } : undefined,
    { dedupingInterval: 60_000 },
  );

  const commands = useMemo<SlashCommand[]>(() => {
    const raw = data?.commands ?? [];
    return raw
      .map((e) => ({
        name: e.name.replace(/^\//, ""),
        description: e.description,
        category: e.category ?? "tools",
        source: e.source,
        acceptsArgs: Boolean(e.acceptsArgs),
        args: e.args,
        aliases: e.textAliases,
      }))
      .filter((c) => !HIDDEN_COMMANDS.has(c.name));
  }, [data]);

  return { commands, isLoading };
}
