import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
const talk = vi.hoisted(() => ({ on: false }));
vi.mock("../TalkSessionProvider", () => ({ useTalkSession: () => talk }));
import { PhoneVoiceSettings } from "../PhoneVoiceSettings";
beforeEach(() => { talk.on = false; });

it("plays same-model static samples only by user gesture and stops replaced players", () => {
  const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  const set = vi.fn();
  const { rerender, unmount } = render(<PhoneVoiceSettings c={{ voice: "Warm", spanish: true }} set={set} />);
  const first = screen.getByLabelText("Voice sample player");
  expect(first).toHaveAttribute("src", "/voice-samples/gpt-live-1-marin-en.wav");
  expect(first).not.toHaveAttribute("autoplay");
  fireEvent.change(screen.getByLabelText("Sample language"), { target: { value: "es" } });
  expect(pause).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("Voice sample player")).toHaveAttribute("src", "/voice-samples/gpt-live-1-marin-es.wav");
  rerender(<PhoneVoiceSettings c={{ voice: "willow", spanish: true }} set={set} />);
  expect(pause).toHaveBeenCalledTimes(2);
  expect(screen.getByLabelText("Voice sample player")).toHaveAttribute("src", "/voice-samples/gpt-live-1-willow-es.wav");
  talk.on = true;
  rerender(<PhoneVoiceSettings c={{ voice: "willow", spanish: true }} set={set} />);
  expect(screen.queryByLabelText("Voice sample player")).toBeNull();
  expect(pause).toHaveBeenCalledTimes(3);
  expect(screen.getByText(/End your Live conversation/)).toBeInTheDocument();
  unmount(); pause.mockRestore();
});

it("surfaces audio errors and offers a user-initiated reload", () => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  const load = vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  render(<PhoneVoiceSettings c={{ voice: "marin", spanish: false }} set={vi.fn()} />);
  fireEvent.loadStart(screen.getByLabelText("Voice sample player"));
  expect(screen.getByRole("status")).toHaveTextContent("Loading voice sample");
  fireEvent.error(screen.getByLabelText("Voice sample player"));
  expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t load this voice sample.");
  fireEvent.click(screen.getByRole("button", { name: "Reload voice sample" }));
  expect(load).toHaveBeenCalledOnce();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("a sample that is only suspended (preload none) stops saying Loading", () => {
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  render(<PhoneVoiceSettings c={{ voice: "marin", spanish: false }} set={vi.fn()} />);
  fireEvent.loadStart(screen.getByLabelText("Voice sample player"));
  expect(screen.getByRole("status")).toHaveTextContent("Loading voice sample");
  fireEvent.suspend(screen.getByLabelText("Voice sample player"));
  expect(screen.queryByRole("status")).toBeNull();
});

it("a load that never finishes falls back to the reload button", () => {
  vi.useFakeTimers();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  render(<PhoneVoiceSettings c={{ voice: "marin", spanish: false }} set={vi.fn()} />);
  fireEvent.loadStart(screen.getByLabelText("Voice sample player"));
  act(() => { vi.advanceTimersByTime(10_000); });
  expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t load this voice sample.");
  vi.useRealTimers();
});
