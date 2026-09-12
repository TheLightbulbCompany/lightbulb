import { Stack } from "expo-router";

import { colors, fonts } from "../../../config/theme";

// Its own Stack so a thread pushes over the conversation, the way teams/ and
// control/ already do. The (app) Tabs layout hides its header for this route.
export default function RoomLayout() {
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: colors.cream },
        headerShadowVisible: false,
        headerTitleStyle: { fontFamily: fonts.sansSemiBold, fontSize: 17, color: colors.ink },
        headerTintColor: colors.forest,
        contentStyle: { backgroundColor: colors.cream },
      }}
    />
  );
}
