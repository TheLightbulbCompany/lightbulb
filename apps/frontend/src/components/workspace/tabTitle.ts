import type { WorkspaceSnapshot } from "./workspaceController";

/** The browser tab names where you are: "Home · Lightbulb", "<project> · Lightbulb", "Activity · Lightbulb".
 *  In Chat the place is the conversation on screen, so its name stands in for the place's. */
export function placeTabTitle(snapshot: Pick<WorkspaceSnapshot, "place" | "main">, chatName: string | null): string {
  const where = snapshot.place === "chat" ? chatName : snapshot.main?.title;
  // The Lightbulb DM is named "Lightbulb": "Lightbulb · Lightbulb" said it twice (W13).
  return where && where !== "Lightbulb" ? `${where} · Lightbulb` : "Lightbulb";
}
