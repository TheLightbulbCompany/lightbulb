import { Redirect } from "expo-router";

// Auth guard lands in Phase 1 (Clerk). Until then, route straight to the
// app shell so the scaffold is navigable in Expo Go.
export default function Index() {
  return <Redirect href="/(app)/chat" />;
}
