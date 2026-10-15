import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ActivityItem } from "@lightbulb/shared";
import { ForYou, type ForYouProps } from "../ForYou";
import { approval, attention, failedRun, loginAsk, maya, message } from "./fixtures";

vi.mock("@/components/teams/inbox/DecisionResolver", () => ({
  DecisionResolver: ({ decisionId }: { decisionId: string }) => <div>Decision card {decisionId}</div>,
}));

function setup(over: Partial<ForYouProps> = {}) {
  const props: ForYouProps = {
    activityItems: [approval, message("m1")], attentionItems: [], loading: false, error: false, paperclipAvailable: true,
    onRunAction: vi.fn().mockResolvedValue(undefined), onNavigate: vi.fn(), people: [], agents: [], ...over,
  };
  render(<ForYou {...props} />);
  return props;
}

const group = (name: string) => screen.getByRole("region", { name: new RegExp(`^${name}`) });

describe("ForYou", () => {
  it("counts what is open and groups it by verb with one action per row", async () => {
    const props = setup({ activityItems: [approval, message("m1"), failedRun] });
    expect(screen.getByRole("heading", { name: "For you · 3 open" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["Decide 1", "Reply 1", "Do 1"]);
    await userEvent.click(within(group("Decide")).getByRole("button", { name: "Approve" }));
    expect(props.onRunAction).toHaveBeenCalledWith(approval.id, "approve", {});
    await userEvent.click(within(group("Do")).getByRole("button", { name: "Open run" }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "run", id: "r1", roomId: null });
  });

  it("replies inline through the item's own action", async () => {
    const props = setup({ activityItems: [message("m1")] });
    await userEvent.click(screen.getByRole("button", { name: "Reply" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Reply" }), "on it");
    await userEvent.click(screen.getAllByRole("button", { name: "Reply" }).at(-1)!);
    expect(props.onRunAction).toHaveBeenCalledWith("mention:m1", "reply", { note: "on it", value: "on it" });
  });

  it("opens the item in context from its line", async () => {
    const props = setup({ activityItems: [message("m1")] });
    await userEvent.click(screen.getByRole("button", { name: /Maya message m1/ }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "thread", id: "m1", roomId: "room-1" });
  });

  it("collapses one source's items into one row that opens in place", async () => {
    setup({ activityItems: [message("m1"), message("m2"), message("m3")] });
    const reply = group("Reply");
    expect(within(reply).getByText(/3 messages/)).toBeInTheDocument();
    expect(within(reply).queryByText("message m1")).not.toBeInTheDocument();
    await userEvent.click(within(reply).getByRole("button", { name: "Show" }));
    expect(within(reply).getByText("message m1")).toBeInTheDocument();
    expect(within(reply).getByText("message m3")).toBeInTheDocument();
  });

  it("shows a group's top 3 rows, then N more", async () => {
    const people = ["Ann", "Bo", "Cy", "Di", "Ed"].map((name) => message(name, { kind: "human", id: `u-${name}`, name }));
    setup({ activityItems: people });
    const reply = group("Reply");
    expect(within(reply).getAllByRole("listitem")).toHaveLength(3);
    await userEvent.click(within(reply).getByRole("button", { name: "2 more" }));
    expect(within(reply).getAllByRole("listitem")).toHaveLength(5);
  });

  it("says so when everything is done", () => {
    setup({ activityItems: [message("m1", maya, true), { ...approval, state: "resolved" } as ActivityItem] });
    expect(screen.getByText("You're all caught up.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "For you" })).toBeInTheDocument();
  });

  it("answers an agent's decision with the existing decision card, and opens a blocked task", async () => {
    const props = setup({ activityItems: [], attentionItems: [attention("d1", "decision"), attention("b1", "blocker_attention", "Ops")] });
    await userEvent.click(within(group("Decide")).getByRole("button", { name: "Answer" }));
    expect(screen.getByText("Decision card subject-d1")).toBeInTheDocument();
    await userEvent.click(within(group("Do")).getByRole("button", { name: "Open" }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "issue", id: "issue-b1", roomId: null });
  });

  it("takes a login an agent asked for in a masked form and posts it as the value only", async () => {
    const props = setup({ activityItems: [loginAsk] });
    await userEvent.click(screen.getByRole("button", { name: "Add login" }));
    expect(screen.getByText("Saved to the workspace's website logins.")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox", { name: "Username or email" }), "me@x.com");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getAllByRole("button", { name: "Add login" }).at(-1)!);
    expect(props.onRunAction).toHaveBeenCalledWith("login:abc", "provide", { value: JSON.stringify({ username: "me@x.com", password: "pw" }) });
  });

  it("keeps the item and says why when its action fails", async () => {
    setup({ activityItems: [approval], onRunAction: vi.fn().mockRejectedValue(new Error("Already decided")) });
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Already decided");
  });

  it("opens and highlights the item a notification points at, even in a collapsed row", () => {
    setup({ activityItems: [message("m1"), message("m2")], focusId: "mention:m2" });
    expect(document.querySelector('[data-for-you-id="mention:m2"]')).toHaveAttribute("data-highlighted", "true");
  });
});
