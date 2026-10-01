import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { VoiceCallView } from "@lightbulb/shared";

const base: VoiceCallView = {
  state: "live", deadline: null, error: null, kind: "dm", voiceName: "Finance",
  team: [{ id: "fin", name: "Finance" }], working: [], pending: 0, captions: [], muted: false,
};
let script: VoiceCallView = base;
let out = 0;
let emit: (v: VoiceCallView) => void = () => {};
const start = vi.fn(async () => {});
const hangUp = vi.fn(async () => {});
const setMuted = vi.fn();

vi.mock("@lightbulb/shared", async (orig) => ({
  ...(await orig<object>()),
  VoiceCallSession: class {
    start = start; hangUp = hangUp; setMuted = setMuted;
    levels() { return { mic: 0, out }; }
    subscribe(l: (v: VoiceCallView) => void) { emit = l; l(script); return () => {}; }
  },
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: vi.fn() }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ onEvent: () => () => {} }) }));

import { VoiceCallProvider, useVoiceCall } from "../VoiceCallProvider";
import { VoiceOrb } from "../VoiceOrb";

const ROOM: VoiceCallView = {
  ...base, kind: "room", voiceName: "CEO",
  team: [{ id: "ceo", name: "CEO" }, { id: "fin", name: "Finance" }, { id: "gro", name: "Growth" }],
};

function StartButton() {
  const { start } = useVoiceCall();
  return <button type="button" onClick={() => start({ agentId: "fin", agentName: "Finance" })}>Talk</button>;
}

function renderCall(view: VoiceCallView, header = true) {
  script = view;
  const tree = (withHeader: boolean) => (
    <VoiceCallProvider>
      {withHeader && <StartButton />}
      <VoiceOrb />
    </VoiceCallProvider>
  );
  const r = render(tree(header));
  fireEvent.click(screen.getByRole("button", { name: "Talk" }));
  return { ...r, dropHeader: () => r.rerender(tree(false)) };
}

let frames: FrameRequestCallback[] = [];
beforeEach(() => {
  out = 0;
  frames = [];
  start.mockClear(); hangUp.mockClear(); setMuted.mockClear();
  vi.stubGlobal("requestAnimationFrame", vi.fn((cb: FrameRequestCallback) => { frames.push(cb); return frames.length; }));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
});
afterEach(() => { vi.unstubAllGlobals(); });

const flushFrame = () => act(() => { const due = frames; frames = []; due.forEach((cb) => cb(0)); });

describe("VoiceOrb", () => {
  it("renders nothing without a call", () => {
    const { container } = render(<VoiceCallProvider><VoiceOrb /></VoiceCallProvider>);
    expect(container.querySelector(".voice-orb")).toBeNull();
  });

  it("collapsed status: Listening…", () => {
    renderCall(base);
    expect(screen.getByText("Listening…")).toBeTruthy();
  });

  it("collapsed status: Finance is working", () => {
    renderCall({ ...ROOM, working: [{ agentId: "fin", label: "browser" }] });
    expect(screen.getByText("Finance is working")).toBeTruthy();
  });

  it("collapsed status: Speaking when the output level is above 0.15", () => {
    renderCall(base);
    out = 0.5;
    flushFrame();
    expect(screen.getByText("Speaking")).toBeTruthy();
    expect(document.querySelector<HTMLElement>(".voice-orb-core")!.style.getPropertyValue("--level")).toBe("0.5");
  });

  it("holds Speaking for 400 ms across a gap between syllables", () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(10_000);
    renderCall(base);
    out = 0.5;
    flushFrame();
    out = 0.02;
    now.mockReturnValue(10_390);
    flushFrame();
    expect(screen.getByText("Speaking")).toBeTruthy();
    now.mockReturnValue(10_400);
    flushFrame();
    expect(screen.getByText("Listening…")).toBeTruthy();
    now.mockRestore();
  });

  it("does not restart the level loop when the view changes", () => {
    renderCall(base);
    act(() => emit({ ...base, captions: [{ role: "you", text: "hi" }] }));
    act(() => emit({ ...base, captions: [{ role: "you", text: "hi there" }] }));
    expect(cancelAnimationFrame).not.toHaveBeenCalled();
    expect(requestAnimationFrame).toHaveBeenCalledTimes(1);
  });

  it("Escape collapses the expanded panel; the toggle controls it", () => {
    renderCall({ ...base, captions: [{ role: "you", text: "hello" }] });
    const toggle = screen.getByRole("button", { name: "Voice call" });
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(document.getElementById(toggle.getAttribute("aria-controls")!)).not.toBeNull();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("hello")).toBeNull();
  });

  it("follows a change to the reduced-motion setting", () => {
    const original = window.matchMedia;
    let matches = false;
    let onChange: () => void = () => {};
    window.matchMedia = ((q: string) => ({ get matches() { return matches; }, media: q, addEventListener: (_: string, cb: () => void) => { onChange = cb; }, removeEventListener() {} })) as unknown as typeof window.matchMedia;
    try {
      renderCall(base);
      const orb = document.querySelector(".voice-orb")!;
      expect(orb.getAttribute("data-motion")).toBe("full");
      matches = true;
      act(() => onChange());
      expect(orb.getAttribute("data-motion")).toBe("reduced");
      expect(cancelAnimationFrame).toHaveBeenCalled();
    } finally {
      window.matchMedia = original;
    }
  });

  it("collapsed status: Thinking… while a delegation has no frame", () => {
    renderCall({ ...base, pending: 1 });
    expect(screen.getByText("Thinking…")).toBeTruthy();
  });

  it("DM shows one avatar with the agent's initials inside the orb", () => {
    renderCall(base);
    const avatars = document.querySelectorAll(".voice-orb-core [role='img']");
    expect(avatars).toHaveLength(1);
    expect(avatars[0].textContent).toBe("FI");
    expect(document.querySelectorAll(".voice-orb-ring-avatar")).toHaveLength(0);
  });

  it("room rings the orb with the team and marks the working one", () => {
    renderCall({ ...ROOM, working: [{ agentId: "fin", label: "browser" }] });
    const ring = document.querySelectorAll(".voice-orb-ring-avatar");
    expect(ring).toHaveLength(3);
    const working = [...ring].filter((el) => el.getAttribute("data-working") === "true");
    expect(working).toHaveLength(1);
    expect(working[0].querySelector("[role='img']")!.getAttribute("aria-label")).toBe("Finance");
  });

  it("expands to captions, chip, time left, Mute and Hang up", () => {
    vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    renderCall({
      ...ROOM, deadline: 1_000_000 + 125_000,
      working: [{ agentId: "fin", label: "browser" }],
      captions: [{ role: "you", text: "What's our MRR?" }, { role: "agent", text: "Checking with Finance." }],
    });
    fireEvent.click(screen.getByRole("button", { name: "Voice call" }));
    expect(screen.getByText("What's our MRR?")).toBeTruthy();
    expect(screen.getByText("Checking with Finance.")).toBeTruthy();
    expect(screen.getByText("Finance — browser")).toBeTruthy();
    expect(screen.getByText("2:05 left")).toBeTruthy();
    const mute = screen.getByRole("button", { name: "Mute" });
    expect(mute.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(mute);
    expect(setMuted).toHaveBeenCalledWith(true);
    fireEvent.click(screen.getByRole("button", { name: "Hang up" }));
    expect(hangUp).toHaveBeenCalled();
    vi.mocked(Date.now).mockRestore();
  });

  it("shows errors in plain words", () => {
    renderCall({ ...base, state: "failed", error: "no_agents" });
    expect(screen.getByText("This channel has no agents to talk to.")).toBeTruthy();
  });

  it("shows v1 errors in plain words", () => {
    renderCall({ ...base, state: "failed", error: "voice_busy" });
    expect(screen.getByText("Someone in your workspace is already on a free call.")).toBeTruthy();
  });

  it("keeps the call when the header unmounts", () => {
    const { dropHeader, unmount } = renderCall(base);
    expect(start).toHaveBeenCalledWith({ agentId: "fin", agentName: "Finance" });
    dropHeader();
    expect(hangUp).not.toHaveBeenCalled();
    expect(screen.getByText("Listening…")).toBeTruthy();
    unmount();
    expect(hangUp).toHaveBeenCalled();
  });

  it("reduced motion: static orb, no animation frame loop", () => {
    const original = window.matchMedia;
    window.matchMedia = ((q: string) => ({ matches: q === "(prefers-reduced-motion: reduce)", media: q, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
    try {
      renderCall(base);
      expect(document.querySelector(".voice-orb")!.getAttribute("data-motion")).toBe("reduced");
      expect(requestAnimationFrame).not.toHaveBeenCalled();
    } finally {
      window.matchMedia = original;
    }
  });
});
