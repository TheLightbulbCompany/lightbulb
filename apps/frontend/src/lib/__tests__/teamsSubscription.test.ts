import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  acquireTeamsEvents,
  releaseTeamsEvents,
  resubscribeTeamsEvents,
  _resetTeamsEventsForTests,
} from "../teamsSubscription";

describe("teamsSubscription refcounting", () => {
  const send = vi.fn(() => true);

  beforeEach(() => {
    _resetTeamsEventsForTests();
    send.mockClear();
  });

  it("acquire alone never sends — subscribe attempts are connection-driven", () => {
    acquireTeamsEvents();
    acquireTeamsEvents();
    expect(send).not.toHaveBeenCalled();
  });

  it("resubscribe sends only while held", () => {
    resubscribeTeamsEvents(send);
    expect(send).not.toHaveBeenCalled();
    acquireTeamsEvents();
    resubscribeTeamsEvents(send);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({ type: "teams.subscribe" });
  });

  it("unsubscribes on the 1→0 transition only", () => {
    acquireTeamsEvents();
    acquireTeamsEvents();
    releaseTeamsEvents(send);
    expect(send).not.toHaveBeenCalled();
    releaseTeamsEvents(send);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({ type: "teams.unsubscribe" });
  });

  it("release at zero is a no-op and does not underflow", () => {
    releaseTeamsEvents(send);
    expect(send).not.toHaveBeenCalled();
    acquireTeamsEvents();
    releaseTeamsEvents(send);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({ type: "teams.unsubscribe" });
  });
});
