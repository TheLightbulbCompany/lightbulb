import {
  checkPhoto,
  draftKey,
  readDraft,
  roomIdsWithDrafts,
  saveDraft,
  type StagedAttachment,
} from "../composerDraft";

const uploaded = (overrides: Partial<StagedAttachment> = {}): StagedAttachment => ({
  key: "k",
  name: "a.png",
  contentType: "image/png",
  uri: "file:///a.png",
  size: 10,
  attachmentId: "att-1",
  failed: false,
  ...overrides,
});

describe("composer drafts", () => {
  // The point of the whole exercise: backing out of a room used to throw an
  // unsent message away, because the text lived in the composer's useState.
  it("gives back what was stored", () => {
    const key = draftKey("owner", "room-1", null);
    saveDraft(key, { text: "half a thought", attachments: [] });
    expect(readDraft(key).text).toBe("half a thought");
  });

  it("drafts a thread separately from the room it belongs to", () => {
    saveDraft(draftKey("owner", "room-2", null), { text: "for the room", attachments: [] });
    expect(readDraft(draftKey("owner", "room-2", "msg-1")).text).toBe("");
  });

  it("scopes a draft to whoever is signed in", () => {
    saveDraft(draftKey("owner-a", "room-3", null), { text: "private", attachments: [] });
    expect(readDraft(draftKey("owner-b", "room-3", null)).text).toBe("");
  });

  it("forgets an emptied draft rather than storing a blank one", () => {
    const key = draftKey("owner", "room-4", null);
    saveDraft(key, { text: "typed", attachments: [] });
    saveDraft(key, { text: "   ", attachments: [] });
    expect(roomIdsWithDrafts("owner").has("room-4")).toBe(false);
  });

  // A photo that reached the server has an id, which is what makes it
  // resendable later; one that did not has nothing to send, so it is not kept
  // and the reader never comes back to a chip that cannot go out.
  it("keeps an uploaded photo and drops one that never landed", () => {
    const key = draftKey("owner", "room-5", null);
    saveDraft(key, {
      text: "",
      attachments: [
        uploaded(),
        uploaded({ key: "in-flight", attachmentId: null }),
        uploaded({ key: "failed", attachmentId: null, failed: true }),
      ],
    });
    expect(readDraft(key).attachments.map((item) => item.key)).toEqual(["k"]);
  });

  it("keeps a photo-only draft, with no text at all", () => {
    const key = draftKey("owner", "room-6", null);
    saveDraft(key, { text: "", attachments: [uploaded()] });
    expect(roomIdsWithDrafts("owner").has("room-6")).toBe(true);
  });

  it("lists the rooms holding a draft, for the room list's tag", () => {
    saveDraft(draftKey("tagged", "room-tagged", null), { text: "unsent", attachments: [] });
    expect(roomIdsWithDrafts("tagged").has("room-tagged")).toBe(true);
    expect(roomIdsWithDrafts("someone-else").has("room-tagged")).toBe(false);
  });
});

describe("checkPhoto", () => {
  // The messaging endpoint caps an image at 5MB. The composer used to check a
  // 10MB cap belonging to an unrelated endpoint, so a 6MB photo passed here,
  // failed on the server, and was reported to the reader as a network problem.
  it("rejects an image past the messaging endpoint's own cap, with its size", () => {
    const { contentType, error } = checkPhoto("image/png", 6 * 1024 * 1024);
    expect(contentType).toBeNull();
    expect(error).toMatch(/6\.0MB/);
    expect(error).toMatch(/limit is 5MB/);
  });

  it("accepts an image within the cap", () => {
    expect(checkPhoto("image/jpeg", 4 * 1024 * 1024)).toEqual({
      contentType: "image/jpeg",
      error: null,
    });
  });

  // HEIC is what an iPhone shoots, and the endpoint does not take it. Saying so
  // beats the upload failing and being blamed on the connection.
  it("rejects a format the endpoint does not accept", () => {
    const { contentType, error } = checkPhoto("image/heic", 100);
    expect(contentType).toBeNull();
    expect(error).toMatch(/format we can't send/);
  });
});
