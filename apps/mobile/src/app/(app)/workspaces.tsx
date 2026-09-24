import { router } from "expo-router";

import { WorkspaceList } from "../../components/workspace/WorkspaceGate";

/** Settings → Switch workspace. After a switch, Home shows the new workspace. */
export default function Workspaces() {
  return <WorkspaceList onSwitched={() => router.dismissTo("/(app)/(tabs)/home")} />;
}
