import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { CardContext, type CardContextValue, useCardPressState } from "@/components/voice/openui/CardContext";
import { OpenUICard } from "@/components/voice/openui/OpenUICard";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const ctx = (over: Partial<CardContextValue> = {}): CardContextValue => ({ messageId: "card", roomId: "room", authorId: "a", threadRootMessageId: null, attachments: [], answered: null, pending: false, error: null, retry: null, ...over });

// The press state is the message's (RoomMessageItem holds it); this stands in for the message.
function Card({ program, onReply, answered = null, ...over }: { program: string; onReply?: (text: string, clientMessageId: string) => Promise<void>; answered?: string | null } & Partial<CardContextValue>) {
  const press = useCardPressState(onReply, answered);
  return <CardContext.Provider value={{ ...ctx(over), ...press }}><OpenUICard program={program} /></CardContext.Provider>;
}

it("Metric, Callout, Steps and Sources read as data, not prose", () => {
  render(<Card program={[
    'root = Stack([m, c, s, src])',
    'm = Metric("MRR", "$4,200", "+12%")',
    'c = Callout("Trial ends Friday", "warn")',
    's = Steps([{label: "Draft", status: "done"}, {label: "Review", status: "doing"}, {label: "Send"}])',
    'src = Sources([{title: "Stripe docs", url: "https://stripe.com/docs"}, {title: "bad", url: "javascript:alert(1)"}])',
  ].join("\n")} />);
  expect(screen.getByText("$4,200")).toBeInTheDocument();
  expect(screen.getByText("+12%")).toBeInTheDocument();
  expect(screen.getByRole("note")).toHaveTextContent("Trial ends Friday");
  const steps = screen.getByRole("list", { name: "Steps" });
  expect(within(steps).getAllByRole("listitem").map((li) => li.getAttribute("data-status"))).toEqual(["done", "doing", "todo"]);
  expect(screen.getByRole("link", { name: "Stripe docs" })).toHaveAttribute("href", "https://stripe.com/docs");
  expect(screen.queryByRole("link", { name: "bad" })).toBeNull();
});

it("Table draws columns as rows", () => {
  render(<Card program={'root = Stack([t])\nt = Table([Col("Plan", ["Starter", "Studio"]), Col("Price", [49, 149])])'} />);
  const rows = screen.getAllByRole("row");
  expect(rows).toHaveLength(3);
  expect(within(rows[0]).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Plan", "Price"]);
  expect(within(rows[2]).getAllByRole("cell").map((c) => c.textContent)).toEqual(["Studio", "149"]);
});

it("EditableTable submits the edited rows as a markdown table", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  render(<Card onReply={onReply} program={'root = Stack([t])\nt = EditableTable(["Plan", "Price"], [["Starter", "49"]], "Send prices")'} />);
  const cell = screen.getByRole("textbox", { name: "Price, row 1" });
  await userEvent.clear(cell);
  await userEvent.type(cell, "59");
  await userEvent.click(screen.getByRole("button", { name: "Send prices" }));
  expect(onReply).toHaveBeenCalledWith("| Plan | Price |\n| --- | --- |\n| Starter | 59 |", expect.any(String));
});

it("Image shows only the message's own attachment", async () => {
  const attachmentUrl = vi.fn().mockResolvedValue({ url: "blob:one" });
  render(<Card attachments={[{ id: "att-1", fileName: "chart.png", contentType: "image/png", size: 10 }]} attachmentUrl={attachmentUrl}
    program={'root = Stack([a, b])\na = Image("chart.png", "Q3 revenue")\nb = Image("https://evil.example/x.png", "Remote")'} />);
  expect(await screen.findByRole("img", { name: "Q3 revenue" })).toHaveAttribute("src", "blob:one");
  expect(screen.queryByRole("img", { name: "Remote" })).toBeNull();
  expect(screen.getByText("Remote")).toBeInTheDocument();
});

it("a source opens in a new tab; an unsafe one stays text", () => {
  render(<Card program={'root = Stack([src])\nsrc = Sources([{title: "Bad", url: "javascript:alert(1)"}, {title: "Stripe docs", url: "https://stripe.com/docs"}])'} />);
  expect(screen.queryByRole("link", { name: "Bad" })).toBeNull();
  expect(screen.getByText("Bad")).toBeInTheDocument();
  const ext = screen.getByRole("link", { name: "Stripe docs" });
  expect(ext).toHaveAttribute("href", "https://stripe.com/docs");
  expect(ext).toHaveAttribute("target", "_blank");
});

it("charts carry a title and an accessible summary", () => {
  // jsdom has no layout, so recharts warns that its container is 0x0; that is expected here.
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  try {
    render(<Card program={'root = Stack([c])\nc = BarChart(["Jan", "Feb"], [10, 20], "Signups")'} />);
    expect(screen.getByRole("figure", { name: "Signups" })).toBeInTheDocument();
    expect(screen.getByText("Jan 10, Feb 20")).toBeInTheDocument();
  } finally {
    warn.mockRestore();
  }
});
