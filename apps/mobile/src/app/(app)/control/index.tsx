import { Section } from "@expo/ui/swift-ui";
import type { SFSymbol } from "expo-symbols";
import { router, type Href } from "expo-router";

import { GroupedList, NavigationRow } from "../../../components/ui/GroupedList";

interface PanelRow {
  key: string;
  label: string;
  description: string;
  icon: SFSymbol;
  /** Nested-Stack route pushed on tap. */
  route: Href;
}

// Mirrors the web ControlSidebar NAV_ITEMS (w3-control-contract §"Actual
// NAV_ITEMS") plus a Marketplace entry. All panels are implemented. Dreams +
// Skill Proposals are omitted — low-frequency, need their own memory/proposal
// data plumbing, out of scope for this build.
const PANELS: PanelRow[] = [
  {
    key: "overview",
    label: "Overview",
    description: "Health, plan, and live counts",
    icon: "waveform.path.ecg",
    route: "/(app)/control/overview",
  },
  {
    key: "agents",
    label: "Agents",
    description: "Your roster and per-agent settings",
    icon: "person.2",
    route: "/(app)/control/agents",
  },
  {
    key: "skills",
    label: "Skills",
    description: "Installed and available skills",
    icon: "bolt",
    route: "/(app)/control/skills",
  },
  {
    key: "sessions",
    label: "Sessions",
    description: "Recent conversations and tokens",
    icon: "square.3.layers.3d",
    route: "/(app)/control/sessions",
  },
  {
    key: "channels",
    label: "Channels",
    description: "Telegram, Discord, and Slack status",
    icon: "dot.radiowaves.left.and.right",
    route: "/(app)/control/channels",
  },
  {
    key: "usage",
    label: "Usage",
    description: "Token spend this billing period",
    icon: "chart.bar",
    route: "/(app)/control/usage",
  },
  {
    key: "llm",
    label: "Model & Credits",
    description: "Provider, model, and Bedrock credits",
    icon: "cpu",
    route: "/(app)/control/llm",
  },
  {
    key: "integrations",
    label: "Integrations",
    description: "Connections, MCP servers, and plugins",
    icon: "square.grid.2x2",
    route: "/(app)/control/integrations",
  },
  {
    key: "marketplace",
    label: "Marketplace",
    description: "Discover and deploy free community agents",
    icon: "bag",
    route: "/(app)/control/marketplace",
  },
];

export default function ControlIndex() {
  return (
    <GroupedList>
      <Section title="Panels">
        {PANELS.map((panel) => (
          <NavigationRow
            key={panel.key}
            title={panel.label}
            subtitle={panel.description}
            icon={panel.icon}
            onPress={() => router.push(panel.route)}
          />
        ))}
      </Section>
    </GroupedList>
  );
}
