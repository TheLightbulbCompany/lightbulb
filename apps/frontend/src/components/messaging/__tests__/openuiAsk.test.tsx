import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { CardContext, useCardPressState } from "../openui/CardContext";
import { OpenUICard } from "../openui/OpenUICard";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const ctx = (over = {}) => ({ messageId: "card", roomId: "room", authorId: "a", threadRootMessageId: null, attachments: [], answered: null, pending: false, error: null, ...over });
// The press state is the message's (RoomMessageItem holds it); this stands in for the message.
function Card({ program, onReply, answered = null }: { program: string; onReply?: (text: string, clientMessageId: string) => Promise<void>; answered?: string | null }) {
  const press = useCardPressState(onReply, answered);
  return <CardContext.Provider value={{ ...ctx(), ...press }}><OpenUICard program={program} /></CardContext.Provider>;
}

const wrap = (program: string, over: { onReply?: (text: string, clientMessageId: string) => Promise<void>; answered?: string | null } = {}) => render(<Card program={program} {...over} />);

it("a single Choice posts on the press", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  wrap('root = Stack([q])\nq = Choice("Which plan first?", ["Starter", "Studio"])', { onReply });
  expect(screen.getByText("Which plan first?")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Studio" }));
  expect(onReply).toHaveBeenCalledWith("Studio", expect.any(String));
});

it("a multi Choice posts the picks on Done", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  wrap('root = Stack([q])\nq = Choice("Which channels?", ["Email", "SMS", "Push"], true)', { onReply });
  await userEvent.click(screen.getByRole("checkbox", { name: "Email" }));
  await userEvent.click(screen.getByRole("checkbox", { name: "Push" }));
  expect(onReply).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(onReply).toHaveBeenCalledWith("Email, Push", expect.any(String));
});

it("options are bounded", () => {
  const options = Array.from({ length: 20 }, (_, i) => `"Option ${i + 1}"`).join(", ");
  wrap(`root = Stack([q])\nq = Choice("Pick", [${options}])`, { onReply: vi.fn() });
  expect(screen.getAllByRole("button").filter((b) => /^Option/.test(b.textContent ?? ""))).toHaveLength(8);
  expect(screen.getByText("+12 more")).toBeInTheDocument();
  wrap('root = Stack([q])\nq = Choice("Anything?", [])', { onReply: vi.fn() });
  expect(screen.getByText("Anything?")).toBeInTheDocument();
});

it("FollowUps are chips that post their text", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  wrap('root = Stack([f])\nf = FollowUps(["Show me the numbers", "Draft the email"])', { onReply });
  await userEvent.click(screen.getByRole("button", { name: "Draft the email" }));
  expect(onReply).toHaveBeenCalledWith("Draft the email", expect.any(String));
});

it("a Form posts Label: value lines", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  wrap(['root = Stack([f])', 'f = Form("Book it", [Input("name", "Name", "Your name"), Select("plan", "Plan", ["Starter", "Studio"]), DatePicker("start", "Start")], "Book")'].join("\n"), { onReply });
  await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "Ada");
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Plan" }), "Studio");
  await userEvent.type(screen.getByLabelText("Start"), "2026-11-01");
  await userEvent.click(screen.getByRole("button", { name: "Book" }));
  expect(onReply).toHaveBeenCalledWith("Name: Ada\nPlan: Studio\nStart: 2026-11-01", expect.any(String));
});

it("an answered Choice shows the pick", () => {
  wrap('root = Stack([q])\nq = Choice("Which plan first?", ["Starter", "Studio"])', { onReply: vi.fn(), answered: "Studio" });
  const group = screen.getByRole("group", { name: "Which plan first?" });
  expect(within(group).getByRole("button", { name: "Studio" })).toHaveAttribute("aria-pressed", "true");
  expect(within(group).getByRole("button", { name: "Starter" })).toBeDisabled();
});

it("a Button posts exactly its label", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  wrap('root = Stack([b])\nb = Buttons([Button("Yes", "primary"), Button("No", "secondary")])', { onReply });
  await userEvent.click(screen.getByRole("button", { name: "Yes" }));
  expect(onReply).toHaveBeenCalledWith("Yes", expect.any(String));
});

it("an answered FollowUps card marks the chip it posted", () => {
  wrap('root = Stack([f])\nf = FollowUps(["Show me the numbers", "Draft the email"])', { onReply: vi.fn(), answered: "Draft the email" });
  expect(screen.getByRole("button", { name: "Draft the email" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "Show me the numbers" })).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByText("You picked Draft the email.")).toBeInTheDocument();
});
