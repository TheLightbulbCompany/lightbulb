import { ApiError } from "@lightbulb/shared";
import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { chatPalette } from "../../../config/theme";
import { MessageMarkdown } from "../MessageMarkdown";
import { CardReplyContext, useCardPressState, type OnReply } from "../openui/CardReplyContext";

const fence = (body: string) => "```openui-lang\n" + body + "\n```";
const CHOICE = fence('root = Stack([q])\nq = Choice("Which plan first?", ["Starter", "Studio"])');
const ctx = (over = {}) => ({ messageId: "card", roomId: "room", authorId: "agent-1", threadRootMessageId: null, attachments: [], answered: null, pending: false, error: null, retry: null, ...over });

// The press state is the message's (MessageGroup holds it); this stands in for the message.
function Card({ text, onReply, answered = null }: { text: string; onReply?: OnReply; answered?: string | null }) {
  const press = useCardPressState(onReply, answered);
  return (
    <CardReplyContext.Provider value={{ ...ctx(), ...press }}>
      <MessageMarkdown text={text} palette={chatPalette.light} />
    </CardReplyContext.Provider>
  );
}

it("renders a Choice from an agent's fence and posts the press", () => {
  const onReply = jest.fn().mockResolvedValue(undefined);
  render(<Card text={CHOICE} onReply={onReply} />);
  expect(screen.getByText("Which plan first?")).toBeTruthy();
  fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  expect(onReply).toHaveBeenCalledWith("Studio", expect.any(String));
});

it("a fence with no card context stays code", () => {
  render(<MessageMarkdown text={fence('root = Stack([t])\nt = Text("hi")')} palette={chatPalette.light} />);
  expect(screen.getByText(/root = Stack/)).toBeTruthy();
});

it("an answered card is read-only and shows the pick", () => {
  render(<Card text={CHOICE} onReply={jest.fn()} answered="Studio" />);
  expect(screen.getByRole("button", { name: "Studio", disabled: true })).toBeTruthy();
  expect(screen.getByText("You picked Studio.")).toBeTruthy();
});

it("charts fall back to a table on the phone", () => {
  render(<Card text={fence('root = Stack([c])\nc = BarChart(["Jan", "Feb"], [10, 20], "Signups")')} />);
  expect(screen.getByText("Signups")).toBeTruthy();
  expect(screen.getByText("Feb")).toBeTruthy();
  expect(screen.getByText("20")).toBeTruthy();
});

it("an unconfirmed send pins the retry: the other option waits and the replay is unchanged", async () => {
  const onReply = jest.fn().mockRejectedValueOnce(new Error("network")).mockResolvedValue(undefined);
  render(<Card text={CHOICE} onReply={onReply} />);
  fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter", disabled: true })).toBeTruthy());
  expect(screen.getByRole("button", { name: "Studio", disabled: false })).toBeTruthy();
  fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await waitFor(() => expect(onReply).toHaveBeenCalledTimes(2));
  expect(onReply.mock.calls[1]).toEqual(onReply.mock.calls[0]);
});

it("a rejected send frees the card: the other option is open and the id is new", async () => {
  const onReply = jest.fn().mockRejectedValueOnce(new ApiError(400, {}, "bad")).mockResolvedValue(undefined);
  render(<Card text={CHOICE} onReply={onReply} />);
  fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter", disabled: false })).toBeTruthy());
  fireEvent.press(screen.getByRole("button", { name: "Starter" }));
  await waitFor(() => expect(onReply).toHaveBeenCalledTimes(2));
  expect(onReply.mock.calls[1][0]).toBe("Starter");
  expect(onReply.mock.calls[1][1]).not.toBe(onReply.mock.calls[0][1]);
});
