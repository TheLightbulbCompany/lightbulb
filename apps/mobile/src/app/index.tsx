import { Redirect } from "expo-router";

// The (app) group's layout enforces auth and bounces unauthenticated users
// to /(auth)/sign-in, so the root can always target Chats.
export default function Index() {
  return <Redirect href="/(app)/(tabs)/chats" />;
}
