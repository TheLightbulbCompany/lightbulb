import { Section } from "@expo/ui/swift-ui";
import type { SFSymbol } from "expo-symbols";
import { router, type Href } from "expo-router";

import { GroupedList, NavigationRow } from "../../../components/ui/GroupedList";

interface MoreRow {
  label: string;
  description: string;
  icon: SFSymbol;
  route: Href;
}

// An iOS tab bar fits four tabs, and Slack's four are Home · DMs · Activity ·
// More. These route trees are PUSHED screens on the (app) stack — not
// hidden tabs — so each one arrives with a real native back button, and this
// screen is the only way a user reaches them.
const ROWS: MoreRow[] = [
  {
    label: "Teams",
    description: "Inbox, issues, and routines",
    icon: "tray",
    route: "/(app)/teams",
  },
  {
    label: "Shared sessions",
    description: "Live Claude and Codex terminals in this workspace",
    icon: "terminal",
    route: "/(app)/sessions",
  },
  {
    label: "Control",
    description: "Agents, skills, usage, and billing",
    icon: "slider.horizontal.3",
    route: "/(app)/control",
  },
  {
    label: "Settings",
    description: "Account, notifications, and sign out",
    icon: "gearshape",
    route: "/(app)/settings",
  },
];

export default function More() {
  return (
    <GroupedList>
      <Section>
        {ROWS.map((row) => (
          <NavigationRow
            key={row.label}
            title={row.label}
            subtitle={row.description}
            icon={row.icon}
            onPress={() => router.push(row.route)}
          />
        ))}
      </Section>
    </GroupedList>
  );
}
