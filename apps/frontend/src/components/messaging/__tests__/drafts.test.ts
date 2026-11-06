import { afterEach, describe, expect, it } from "vitest";
import { clearDraft, draftKey, listDraftRoomIds, readDraft, writeDraft } from "../drafts";

afterEach(() => localStorage.clear());

describe("drafts", () => {
  it("round-trips text and mentions for a room and for a thread separately", () => {
    writeDraft(draftKey("org_1", "room-a", null), { text: "room draft", mentions: [{ kind: "agent", id: "agent-friday" }] });
    writeDraft(draftKey("org_1", "room-a", "msg-1"), { text: "thread draft", mentions: [] });
    expect(readDraft(draftKey("org_1", "room-a", null))).toMatchObject({ text: "room draft", mentions: [{ kind: "agent", id: "agent-friday" }] });
    expect(readDraft(draftKey("org_1", "room-a", "msg-1"))).toMatchObject({ text: "thread draft", mentions: [] });
  });
  it("scopes drafts to the owner", () => {
    writeDraft(draftKey("org_1", "room-a", null), { text: "mine", mentions: [] });
    expect(readDraft(draftKey("org_2", "room-a", null))).toEqual({ text: "", mentions: [], at: 0 });
    expect(listDraftRoomIds("org_2")).toEqual([]);
  });
  it("keeps an emptied draft as a dated clear, and drops the room from the list", () => {
    const key = draftKey("org_1", "room-a", null);
    writeDraft(key, { text: "typing", mentions: [] });
    expect(listDraftRoomIds("org_1")).toEqual(["room-a"]);
    writeDraft(key, { text: "   ", mentions: [] });
    expect(readDraft(key)).toMatchObject({ text: "", mentions: [] });
    expect(listDraftRoomIds("org_1")).toEqual([]);
  });
  it("W17-7: a tab holding an older edit never writes it over a newer draft or a send's clear", () => {
    const key = draftKey("org_1", "dm", null);
    expect(writeDraft(key, { text: "@growth ", mentions: [] }, 1_000)).toBe(true);
    clearDraft(key); // sent in another tab, now
    expect(writeDraft(key, { text: "@growth ", mentions: [] }, 1_000)).toBe(false);
    expect(readDraft(key).text).toBe("");
    expect(writeDraft(key, { text: "new", mentions: [] })).toBe(true);
    expect(readDraft(key).text).toBe("new");
  });
  it("drops clears older than a day when listing", () => {
    const key = draftKey("org_1", "old", null);
    writeDraft(key, { text: "", mentions: [] }, Date.now() - 2 * 24 * 60 * 60 * 1000);
    expect(listDraftRoomIds("org_1")).toEqual([]);
    expect(localStorage.getItem(key)).toBeNull();
  });
  it("lists a room once even when it also has a thread draft, and survives junk", () => {
    writeDraft(draftKey("org_1", "room-a", null), { text: "a", mentions: [] });
    writeDraft(draftKey("org_1", "room-a", "msg-1"), { text: "b", mentions: [] });
    localStorage.setItem("lightbulb.messaging.draft|org_1|room-b|", "not json");
    expect(listDraftRoomIds("org_1")).toEqual(["room-a"]);
  });
  it("reads an absent or corrupt row as an empty draft", () => {
    expect(readDraft(draftKey("org_1", "nope", null))).toEqual({ text: "", mentions: [], at: 0 });
    localStorage.setItem(draftKey("org_1", "bad", null), "{");
    expect(readDraft(draftKey("org_1", "bad", null))).toEqual({ text: "", mentions: [], at: 0 });
  });
});
