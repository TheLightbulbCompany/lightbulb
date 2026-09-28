import { colors, chatPalette } from "../../config/theme";
import { approvalStatusMeta, priorityColor, statusColors } from "../teamsData";

describe("status chips in dark mode", () => {
  it("approval chips reuse the board's dark chips; light stays unchanged", () => {
    expect(approvalStatusMeta("pending", "dark")).toEqual({ label: "Pending", ...statusColors("todo", "dark") });
    expect(approvalStatusMeta("rejected", "dark").color).toBe(chatPalette.dark.alert);
    expect(approvalStatusMeta("approved", "dark").color).toBe(chatPalette.dark.live);
    expect(approvalStatusMeta("pending").color).not.toBe(chatPalette.dark.warn);
  });
});

describe("priorityColor in dark mode (Codex #3341)", () => {
  it("swaps in dark-legible accents for dark; light stays unchanged", () => {
    expect(priorityColor("critical", "dark")).toBe(chatPalette.dark.alert);
    expect(priorityColor("high", "dark")).toBe(chatPalette.dark.alert);
    expect(priorityColor("medium", "dark")).toBe(chatPalette.dark.warn);
    expect(priorityColor("low", "dark")).toBe(chatPalette.dark.fgMute);
    expect(priorityColor("critical")).toBe(colors.priority.critical);
    expect(priorityColor("critical")).not.toBe(chatPalette.dark.alert);
  });

  it("stays null for unset/unknown priority in either scheme", () => {
    expect(priorityColor(null, "dark")).toBeNull();
    expect(priorityColor("urgent", "dark")).toBeNull();
  });
});
