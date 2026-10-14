import { useAuth } from "@clerk/expo";
import { Redirect } from "expo-router";
import { findCeoDm } from "../../lib/phoneHome";
import { useMobileRooms } from "../../lib/rooms";

export default function PhoneHome() {
  const { userId } = useAuth();
  const { rooms, loading } = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  const ceoDm = findCeoDm(rooms, userId);
  if (ceoDm) return <Redirect href={`/(app)/room/${ceoDm.id}`} />;
  if (loading) return null;
  return <Redirect href="/(app)/(tabs)/chats" />;
}