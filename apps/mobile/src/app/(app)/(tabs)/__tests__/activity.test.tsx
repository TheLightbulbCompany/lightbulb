import type { QuestionView } from "@lightbulb/shared";
import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

// Every out-of-scope name a jest.mock factory reaches is `mock`-prefixed and
// read lazily (babel lowers these to `var`; the factories run mid-import).
jest.mock("expo-router", () => ({ useFocusEffect: (cb: () => void) => require("react").useEffect(cb, [cb]), useIsFocused: () => true }));
jest.mock("expo-crypto", () => ({ randomUUID: () => "answer-1" }));
const mockNeedsYou = jest.fn();
const mockAnswer = jest.fn();
jest.mock("../../../../providers/live-events", () => {
  const tasks = { needsYou: () => mockNeedsYou(), answer: (...a: unknown[]) => mockAnswer(...a) };
  return { useLiveEvents: () => ({ tasks }) };
});

import NeedsYouScreen from "../activity";

const q = (over: Partial<QuestionView> = {}): QuestionView => ({
  id: "q1", taskId: "t1", kind: "question", subkind: "didnt_know", title: "Do you take walk-ins?", text: "",
  options: [{ id: "answer", label: "Answer", style: "primary" }], state: "open", answerers: [], canAnswer: true,
  waitingLabel: null, answer: null, answeredBy: null, answeredAt: null, createdAt: new Date().toISOString(), version: 3,
  ...over,
});

beforeEach(() => jest.clearAllMocks());

it("says when nothing needs you", async () => {
  mockNeedsYou.mockResolvedValue({ items: [], next: null });
  await render(<NeedsYouScreen />);
  expect(await screen.findByText("Nothing needs you.")).toBeTruthy();
});

it("answers a didn't-know question with the typed text and re-reads the list", async () => {
  mockNeedsYou.mockResolvedValue({ items: [q()], next: null });
  mockAnswer.mockImplementation(async () => {
    mockNeedsYou.mockResolvedValue({ items: [], next: null });
    return { question: q({ state: "answered" }), next: null };
  });
  await render(<NeedsYouScreen />);
  expect(await screen.findByText("Lightbulb didn't know · now")).toBeTruthy();
  await fireEvent.changeText(screen.getByLabelText("Your answer"), "Yes, until 4pm");
  await fireEvent.press(screen.getByRole("button", { name: "Answer" }));
  expect(mockAnswer).toHaveBeenCalledWith("t1", "q1", { optionId: "answer", clientAnswerId: "answer-1", expectVersion: 3, text: "Yes, until 4pm" });
  await waitFor(() => expect(screen.getByText("Nothing needs you.")).toBeTruthy());
});

it("taps an approval's option and shows a refusal", async () => {
  mockNeedsYou.mockResolvedValue({ items: [q({ kind: "approval", subkind: null, title: "Send the reminder",
    options: [{ id: "deny", label: "Deny", style: "secondary" }, { id: "allow_once", label: "Allow once", style: "primary" }] })], next: null });
  mockAnswer.mockRejectedValue(new Error("offline"));
  await render(<NeedsYouScreen />);
  expect(await screen.findByText("Needs your OK · now")).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Allow once" }));
  expect(mockAnswer.mock.calls[0][2].optionId).toBe("allow_once");
  expect(await screen.findByText("Couldn't send. Try again.")).toBeTruthy();
});

it("shows who a question waits on when the viewer can't answer", async () => {
  mockNeedsYou.mockResolvedValue({ items: [q({ canAnswer: false, waitingLabel: "Waiting on Dana" })], next: null });
  await render(<NeedsYouScreen />);
  expect(await screen.findByText("Waiting on Dana")).toBeTruthy();
  expect(screen.queryByLabelText("Your answer")).toBeNull();
});
