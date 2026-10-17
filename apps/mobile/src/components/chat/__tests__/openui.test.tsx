import { ApiError } from "@lightbulb/shared";
import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { chatPalette } from "../../../config/theme";
import { MessageMarkdown } from "../MessageMarkdown";
import { _resetCardAttemptsForTests, CardReplyContext, useCardPressState, type OnReply } from "../openui/CardReplyContext";

// Native randomUUID is absent under jest (the other suites mock it too); a counter keeps ids distinct.
let mockN = 0;
jest.mock("expo-crypto", () => ({ randomUUID: () => `id-${++mockN}` }));

const fence = (body: string) => "```openui-lang\n" + body + "\n```";
const CHOICE = fence('root = Stack([q])\nq = Choice("Which plan first?", ["Starter", "Studio"])');
const ctx = (over = {}) => ({ messageId: "card", roomId: "room", authorId: "agent-1", threadRootMessageId: null, attachments: [], answered: null, pending: false, error: null, retry: null, ...over });

// The press state is the message's (MessageGroup holds it); this stands in for the message.
function Card({ text, onReply, answered = null, messageId = "card" }: { text: string; onReply?: OnReply; answered?: string | null; messageId?: string }) {
  const press = useCardPressState(messageId, onReply, answered);
  return (
    <CardReplyContext.Provider value={{ ...ctx(), ...press }}>
      <MessageMarkdown text={text} palette={chatPalette.light} />
    </CardReplyContext.Provider>
  );
}

beforeEach(_resetCardAttemptsForTests);

it("renders a Choice from an agent's fence and posts the press", async () => {
  const onReply = jest.fn().mockResolvedValue(undefined);
  await render(<Card text={CHOICE} onReply={onReply} />);
  expect(screen.getByText("Which plan first?")).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  expect(onReply).toHaveBeenCalledWith("Studio", expect.any(String));
});

it("a fence with no card context stays code", async () => {
  await render(<MessageMarkdown text={fence('root = Stack([t])\nt = Text("hi")')} palette={chatPalette.light} />);
  expect(screen.getByText(/root = Stack/)).toBeTruthy();
});

it("an answered card is read-only and shows the pick", async () => {
  await render(<Card text={CHOICE} onReply={jest.fn()} answered="Studio" />);
  expect(screen.getByRole("button", { name: "Studio", disabled: true })).toBeTruthy();
  expect(screen.getByText("You picked Studio.")).toBeTruthy();
});

it("charts fall back to a table on the phone", async () => {
  await render(<Card text={fence('root = Stack([c])\nc = BarChart(["Jan", "Feb"], [10, 20], "Signups")')} />);
  expect(screen.getByText("Signups")).toBeTruthy();
  expect(screen.getByText("Feb")).toBeTruthy();
  expect(screen.getByText("20")).toBeTruthy();
});

it("an unconfirmed send pins the retry: the other option waits and the replay is unchanged", async () => {
  const onReply = jest.fn().mockRejectedValueOnce(new Error("network")).mockResolvedValue(undefined);
  await render(<Card text={CHOICE} onReply={onReply} />);
  await fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter", disabled: true })).toBeTruthy());
  expect(screen.getByRole("button", { name: "Studio", disabled: false })).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await waitFor(() => expect(onReply).toHaveBeenCalledTimes(2));
  expect(onReply.mock.calls[1]).toEqual(onReply.mock.calls[0]);
});

it("a rejected send frees the card: the other option is open and the id is new", async () => {
  const onReply = jest.fn().mockRejectedValueOnce(new ApiError(400, {}, "bad")).mockResolvedValue(undefined);
  await render(<Card text={CHOICE} onReply={onReply} />);
  await fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter", disabled: false })).toBeTruthy());
  await fireEvent.press(screen.getByRole("button", { name: "Starter" }));
  await waitFor(() => expect(onReply).toHaveBeenCalledTimes(2));
  expect(onReply.mock.calls[1][0]).toBe("Starter");
  expect(onReply.mock.calls[1][1]).not.toBe(onReply.mock.calls[0][1]);
});

it("survives the row unmounting: the pinned attempt is replayed unchanged", async () => {
  const onReply = jest.fn().mockRejectedValueOnce(new Error("network")).mockResolvedValue(undefined);
  const first = await render(<Card text={CHOICE} onReply={onReply} messageId="m1" />);
  await fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter", disabled: true })).toBeTruthy());
  await first.unmount();
  await render(<Card text={CHOICE} onReply={onReply} messageId="m1" />);
  expect(screen.getByRole("button", { name: "Starter", disabled: true })).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await waitFor(() => expect(onReply).toHaveBeenCalledTimes(2));
  expect(onReply.mock.calls[1]).toEqual(onReply.mock.calls[0]);
});

it("a multi Choice can replay its pinned attempt after the row remounts", async () => {
  const multi = fence('root = Stack([q])\nq = Choice("Which channels?", ["Email", "SMS"], true)');
  const onReply = jest.fn().mockRejectedValueOnce(new Error("network")).mockResolvedValue(undefined);
  const first = await render(<Card text={multi} onReply={onReply} messageId="m2" />);
  await fireEvent.press(screen.getByRole("button", { name: "Email" }));
  await fireEvent.press(screen.getByRole("button", { name: "Done" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "SMS", disabled: true })).toBeTruthy());
  await first.unmount();
  await render(<Card text={multi} onReply={onReply} messageId="m2" />);
  await fireEvent.press(screen.getByRole("button", { name: "Done", disabled: false }));
  await waitFor(() => expect(onReply).toHaveBeenCalledTimes(2));
  expect(onReply.mock.calls[1]).toEqual(onReply.mock.calls[0]);
});

it("the local marker yields to the canonical answer: deleting the reply reopens the card", async () => {
  const onReply = jest.fn().mockResolvedValue(undefined);
  const view = await render(<Card text={CHOICE} onReply={onReply} messageId="m3" />);
  await fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await waitFor(() => expect(screen.getByText("You picked Studio.")).toBeTruthy());
  await view.rerender(<Card text={CHOICE} onReply={onReply} messageId="m3" answered="Studio" />);
  expect(screen.getByText("You picked Studio.")).toBeTruthy();
  await view.rerender(<Card text={CHOICE} onReply={onReply} messageId="m3" answered={null} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter", disabled: false })).toBeTruthy());
});
