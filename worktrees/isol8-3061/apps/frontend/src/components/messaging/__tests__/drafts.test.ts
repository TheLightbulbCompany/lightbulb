import { afterEach, describe, expect, it } from "vitest";
import { draftKey, listDraftRoomIds, readDraft, writeDraft } from "../drafts";

afterEach(() => localStorage.clear());

describe("drafts", () => {
  it("round-trips text and mentions for a room and for a thread separately", () => {
    writeDraft(draftKey("org_1", "room-a", null), { text: "room draft", mentions: [{ kind: "agent", id: "agent-friday" }] });
    writeDraft(draftKey("org_1", "room-a", "msg-1"), { text: "thread draft", mentions: [] });
    expect(readDraft(draftKey("org_1", "room-a", null))).toEqual({ text: "room draft", mentions: [{ kind: "agent", id: "agent-friday" }] });
    expect(readDraft(draftKey("org_1", "room-a", "msg-1"))).toEqual({ text: "thread draft", mentions: [] });
  });
  it("scopes drafts to the owner", () => {
    writeDraft(draftKey("org_1", "room-a", null), { text: "mine", mentions: [] });
    expect(readDraft(draftKey("org_2", "room-a", null))).toEqual({ text: "", mentions: [] });
    expect(listDraftRoomIds("org_2")).toEqual([]);
  });
  it("removes the row when the draft empties, and drops the room from the list", () => {
    const key = draftKey("org_1", "room-a", null);
    writeDraft(key, { text: "typing", mentions: [] });
    expect(listDraftRoomIds("org_1")).toEqual(["room-a"]);
    writeDraft(key, { text: "   ", mentions: [] });
    expect(localStorage.getItem(key)).toBeNull();
    expect(listDraftRoomIds("org_1")).toEqual([]);
  });
  it("lists a room once even when it also has a thread draft, and survives junk", () => {
    writeDraft(draftKey("org_1", "room-a", null), { text: "a", mentions: [] });
    writeDraft(draftKey("org_1", "room-a", "msg-1"), { text: "b", mentions: [] });
    localStorage.setItem("lightbulb.messaging.draft|org_1|room-b|", "not json");
    expect(listDraftRoomIds("org_1")).toEqual(["room-a"]);
  });
  it("reads an absent or corrupt row as an empty draft", () => {
    expect(readDraft(draftKey("org_1", "nope", null))).toEqual({ text: "", mentions: [] });
    localStorage.setItem(draftKey("org_1", "bad", null), "{");
    expect(readDraft(draftKey("org_1", "bad", null))).toEqual({ text: "", mentions: [] });
  });
});
