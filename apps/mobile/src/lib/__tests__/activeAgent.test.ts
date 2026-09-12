import { claimRoomTarget, getCurrentTarget, setCurrentTarget } from "../activeAgent";

afterEach(() => setCurrentTarget({ agentId: null, sessionId: null, roomId: null, threadId: null }));

describe("claimRoomTarget", () => {
  it("claims the room and clears it on blur", () => {
    const release = claimRoomTarget("r1", null);
    expect(getCurrentTarget()).toMatchObject({ roomId: "r1", threadId: null });
    release();
    expect(getCurrentTarget()).toMatchObject({ roomId: null, threadId: null });
  });

  // A push for the room you are already in stacks a second screen with the
  // IDENTICAL target, so the release cannot compare by value — the older
  // screen's blur would clear the newer screen's claim and banner every message
  // in the room on screen.
  it("does not clear a target a second screen claimed for the same room", () => {
    const releaseFirst = claimRoomTarget("r1", null);
    const releaseSecond = claimRoomTarget("r1", null);
    releaseFirst();
    expect(getCurrentTarget()).toMatchObject({ roomId: "r1", threadId: null });
    releaseSecond();
    expect(getCurrentTarget()).toMatchObject({ roomId: null, threadId: null });
  });

  // expo-router does not order a blurring screen's cleanup against the newly
  // focused screen's claim. An unconditional clear would wipe the thread's
  // claim on the way in, and banner a reply that is on screen.
  it("does not clear a target a newer screen already claimed", () => {
    const releaseRoom = claimRoomTarget("r1", null);
    const releaseThread = claimRoomTarget("r1", "m1");
    releaseRoom();
    expect(getCurrentTarget()).toMatchObject({ roomId: "r1", threadId: "m1" });
    releaseThread();
    expect(getCurrentTarget()).toMatchObject({ roomId: null, threadId: null });
  });
});
