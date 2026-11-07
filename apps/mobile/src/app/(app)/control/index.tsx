import { Section } from "../../../components/ui/swiftui";
import type { SFSymbol } from "expo-symbols";
import { router, type Href } from "expo-router";
import { useColorScheme } from "react-native";

import { GroupedList, NavigationRow } from "../../../components/ui/GroupedList";

interface PanelRow {
  key: string;
  label: string;
  description: string;
  icon: SFSymbol;
  /** Nested-Stack route pushed on tap. */
  route: Href;
}

// Mirrors the web control nav plus a Marketplace entry. Skill Proposals is
// omitted — low-frequency, needs its own proposal data plumbing.
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
    key: "usage",
    label: "Usage",
    description: "Token spend this billing period",
    icon: "chart.bar",
    route: "/(app)/control/usage",
  },
  {
    key: "llm",
    label: "Model & Credits",
    description: "Provider, model, and workspace credits",
    icon: "cpu",
    route: "/(app)/control/llm",
  },

];

export default function ControlIndex() {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  return (
    <GroupedList colorScheme={scheme}>
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
