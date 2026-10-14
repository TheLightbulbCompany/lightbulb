import { Redirect } from "expo-router";

// The (app) group's layout enforces auth. Its index resolves the CEO's DM,
// so a phone opens on a teammate rather than a workspace-shaped inbox.
export default function Index() {
  return <Redirect href="/(app)" />;
}
