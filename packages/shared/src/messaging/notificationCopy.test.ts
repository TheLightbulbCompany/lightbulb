import { describe, expect, it } from "vitest";
import { notificationLine, notificationTitle, roomWhere, runFailureLine, sentenceCase } from "./notificationCopy";

describe("notification copy", () => {
  it("line 1 is who · where", () => {
    expect(notificationTitle("Pulse", roomWhere({ kind: "channel", name: "GTM Strategy" }))).toBe("Pulse · GTM Strategy");
    expect(notificationTitle("Friday", roomWhere({ kind: "dm", name: null }))).toBe("Friday · DM");
    expect(notificationTitle("Pulse", "ISOA-400")).toBe("Pulse · ISOA-400");
    expect(notificationTitle("Pulse", null)).toBe("Pulse");
  });

  it("names every room kind", () => {
    expect(roomWhere({ kind: "group-dm", name: "Launch crew" })).toBe("Launch crew");
    expect(roomWhere({ kind: "group-dm", name: null })).toBe("Group chat");
    expect(roomWhere({ kind: "channel", name: null })).toBe("Project");
    expect(roomWhere(undefined)).toBeNull();
  });

  it("line 2 is the first plain line, clipped", () => {
    expect(notificationLine("\n## Plan\n- step one")).toBe("Plan");
    expect(notificationLine("- **Bold** and `code`")).toBe("Bold and code");
    expect(notificationLine("   ")).toBe("");
    const long = notificationLine("x".repeat(200));
    expect(long).toHaveLength(80);
    expect(long.endsWith("…")).toBe(true);
    expect(notificationLine("a b c d", 5)).toBe("a b…");
  });

  it("capitalizes a verb phrase", () => {
    expect(sentenceCase("wants to hire Designer")).toBe("Wants to hire Designer");
    expect(sentenceCase("")).toBe("");
  });

  it("says why a run failed in plain words", () => {
    expect(runFailureLine("process_lost", "Process lost: pid 12")).toBe("Couldn't finish: the workspace restarted mid-run");
    expect(runFailureLine(null, "Rate limited by provider\nstack…")).toBe("Couldn't finish: Rate limited by provider");
    expect(runFailureLine("constructor", null)).toBe("Couldn't finish: the run hit an error");
  });
});
