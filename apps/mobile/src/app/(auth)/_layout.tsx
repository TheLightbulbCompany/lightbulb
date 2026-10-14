import { useAuth } from "@clerk/expo";
import { Redirect, Stack } from "expo-router";

export default function AuthLayout() {
  // Pending (no workspace chosen yet) counts as signed in: (app)'s WorkspaceGate
  // finishes it. Treated as signed out, sign-in would never move on.
  const { isSignedIn, isLoaded } = useAuth({ treatPendingAsSignedOut: false });
  if (!isLoaded) return null;
  if (isSignedIn) return <Redirect href="/(app)" />;
  return <Stack screenOptions={{ headerShown: false }} />;
}
