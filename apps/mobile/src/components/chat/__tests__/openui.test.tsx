import { Linking } from "react-native";
import { router } from "expo-router";
import { ApiError } from "@lightbulb/shared";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { chatPalette } from "../../../config/theme";
import { MessageMarkdown } from "../MessageMarkdown";
import { _resetCardAttemptsForTests, CardReplyContext, useCardPressState, type OnReply } from "../openui/CardReplyContext";

jest.mock("../../../lib/attachmentFile", () => ({ attachmentFile: jest.fn().mockResolvedValue({ uri: "file:///cache/chart.png" }) }));
import { attachmentFile } from "../../../lib/attachmentFile";
jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

// Native randomUUID is absent under jest (the other suites mock it too); a counter keeps ids distinct.
let mockN = 0;
jest.mock("expo-crypto", () => ({ randomUUID: () => `id-${++mockN}` }));

const fence = (body: string) => "```openui-lang\n" + body + "\n```";
const CHOICE = fence('root = Stack([q])\nq = Choice("Which plan first?", ["Starter", "Studio"])');
const ctx = (over = {}) => ({ messageId: "card", roomId: "room", authorId: "agent-1", threadRootMessageId: null, attachments: [], answered: null, pending: false, error: null, retry: null, ...over });

// The press state is the message's (MessageGroup holds it); this stands in for the message.
function Card({ text, onReply, answered = null, messageId = "card", viewerId = "user_alice" }: { text: string; onReply?: OnReply; answered?: string | null; messageId?: string; viewerId?: string }) {
  const press = useCardPressState(messageId, viewerId, onReply, answered);
  return (
    <CardReplyContext.Provider value={{ ...ctx(), ...press }}>
      <MessageMarkdown text={text} palette={chatPalette.light} />
    </CardReplyContext.Provider>
  );
}

beforeEach(_resetCardAttemptsForTests);
afterEach(() => jest.restoreAllMocks());

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

it("two presses in one frame post once: the second finds the first's attempt in the store", async () => {
  const onReply = jest.fn().mockResolvedValue(undefined);
  let press: ((text: string) => void) | undefined;
  function Probe() {
    press = useCardPressState("m7", "user_alice", onReply, null).press;
    return null;
  }
  await render(<Probe />);
  await act(async () => {
    press?.("Studio");
    press?.("Starter");
  });
  expect(onReply).toHaveBeenCalledTimes(1);
  expect(onReply).toHaveBeenCalledWith("Studio", expect.any(String));
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

it("an unconfirmed attempt yields to the canonical answer too: the pin goes, and a deleted reply reopens every option", async () => {
  const onReply = jest.fn().mockRejectedValueOnce(new Error("timeout"));
  const view = await render(<Card text={CHOICE} onReply={onReply} messageId="m5" />);
  await fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter", disabled: true })).toBeTruthy());
  await view.rerender(<Card text={CHOICE} onReply={onReply} messageId="m5" answered="Studio" />);
  expect(screen.getByRole("button", { name: "Studio", disabled: true })).toBeTruthy();
  await view.rerender(<Card text={CHOICE} onReply={onReply} messageId="m5" answered={null} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter", disabled: false })).toBeTruthy());
  expect(screen.getByRole("button", { name: "Studio", disabled: false })).toBeTruthy();
});

it("a POST that settles after the canonical answer landed writes nothing back", async () => {
  let finish: () => void = () => {};
  const onReply = jest.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
  const view = await render(<Card text={CHOICE} onReply={onReply} messageId="m6" />);
  await fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await view.rerender(<Card text={CHOICE} onReply={onReply} messageId="m6" answered="Studio" />);
  finish();
  await view.rerender(<Card text={CHOICE} onReply={onReply} messageId="m6" answered={null} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter", disabled: false })).toBeTruthy());
  expect(screen.getByRole("button", { name: "Studio", disabled: false })).toBeTruthy();
});

it("a pinned attempt belongs to the person who pressed: the next member in this process starts clean", async () => {
  const onReply = jest.fn().mockRejectedValueOnce(new Error("timeout"));
  const first = await render(<Card text={CHOICE} onReply={onReply} messageId="m7" viewerId="user_alice" />);
  await fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter", disabled: true })).toBeTruthy());
  await first.unmount();
  const second = jest.fn().mockResolvedValue(undefined);
  await render(<Card text={CHOICE} onReply={second} messageId="m7" viewerId="user_bob" />);
  expect(screen.getByRole("button", { name: "Starter", disabled: false })).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Starter" }));
  await waitFor(() => expect(second).toHaveBeenCalledTimes(1));
  expect(second.mock.calls[0][0]).toBe("Starter");
  expect(second.mock.calls[0][1]).not.toBe(onReply.mock.calls[0][1]);
});

it("Sources open a native screen, the web, or nothing", async () => {
  const open = jest.spyOn(Linking, "openURL").mockResolvedValue(true);
  await render(<Card text={fence('root = Stack([s])\ns = Sources([{ title: "Session", url: "https://thelightbulb.company/chat?panel=session&session=s1" }, { title: "Docs", url: "https://example.com/d" }, { title: "Bad", url: "javascript:alert(1)" }])')} />);
  await fireEvent.press(screen.getByText("Session"));
  expect(router.push).toHaveBeenCalledWith({ pathname: "/(app)/session/[sessionId]", params: { sessionId: "s1" } });
  await fireEvent.press(screen.getByText("Docs"));
  expect(open).toHaveBeenCalledWith("https://example.com/d");
  expect(screen.getByText("Bad")).toBeTruthy();
  expect(screen.queryByRole("link", { name: "Bad" })).toBeNull();
});

it("Image shows the message's own attachment and never a URL from the program", async () => {
  const attachmentUrl = jest.fn().mockResolvedValue({ url: "https://s3/x", expiresIn: 60 });
  const program = fence('root = Stack([a, b])\na = Image("chart.png", "Q3 revenue")\nb = Image("https://evil.example/x.png", "Remote")');
  await render(
    <CardReplyContext.Provider value={ctx({ attachmentUrl, attachments: [{ id: "att1", fileName: "chart.png", contentType: "image/png" }] })}>
      <MessageMarkdown text={program} palette={chatPalette.light} />
    </CardReplyContext.Provider>,
  );
  await waitFor(() => expect(screen.getByLabelText("Q3 revenue")).toBeTruthy());
  expect(screen.getByLabelText("Q3 revenue").props.source).toEqual([{ uri: "file:///cache/chart.png" }]);
  expect(screen.getByText("Remote")).toBeTruthy();
  expect(attachmentFile).toHaveBeenCalledWith("room", expect.objectContaining({ id: "att1" }), expect.any(Function));
  expect(screen.queryByLabelText("Remote")).toBeNull();
});
