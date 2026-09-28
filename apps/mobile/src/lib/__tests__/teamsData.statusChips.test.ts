import { chatPalette } from "../../config/theme";
import { approvalStatusMeta, statusColors } from "../teamsData";

describe("status chips in dark mode", () => {
  it("approval chips reuse the board's dark chips; light stays unchanged", () => {
    expect(approvalStatusMeta("pending", "dark")).toEqual({ label: "Pending", ...statusColors("todo", "dark") });
    expect(approvalStatusMeta("rejected", "dark").color).toBe(chatPalette.dark.alert);
    expect(approvalStatusMeta("approved", "dark").color).toBe(chatPalette.dark.live);
    expect(approvalStatusMeta("pending").color).not.toBe(chatPalette.dark.warn);
  });
});
