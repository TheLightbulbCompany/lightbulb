import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { MeetTile } from "../MeetTile";

const A = (id: string, status: string, extra: Record<string, string> = {}) =>
  Object.fromEntries([[`lightbulb.agent.${id}.status`, status], ...Object.entries(extra).map(([k, v]) => [`lightbulb.agent.${id}.${k}`, v])]);
const tile = (attrs: Record<string, string>, agents: Record<string, { id: string; name: string }> = {}) =>
  render(<MeetTile agents={agents} attrs={attrs} notice={null} />);
const rosterNames = (c: HTMLElement) => [...[...c.querySelectorAll("ul")].at(-1)!.querySelectorAll("span.truncate")].map((e) => e.textContent).join("");

it("keeps the roster order fixed, whatever the attribute order or who is active", () => {
  const ids = ["vera", "Atlas", "forge", "Bo"];
  const a = tile(Object.assign({}, ...ids.map((id) => A(id, "idle"))));
  const b = tile(Object.assign({}, ...[...ids].reverse().map((id, i) => A(id, i === 1 ? "speaking" : "working", { working: "x" }))));
  expect(rosterNames(a.container)).toBe("AtlasBoforgevera");
  expect(rosterNames(b.container)).toBe("AtlasBoforgevera");
});

it("puts the speaker on the stage with Speaking", () => {
  const { container } = tile({ ...A("f", "speaking"), "lightbulb.speaking_agent": "f" }, { f: { id: "f", name: "Forge" } });
  expect(screen.getByText("Speaking")).toBeInTheDocument();
  expect(container.querySelector("p.text-6xl")?.textContent).toBe("Forge");
});

it("shows Working: <task> for an agent that holds the floor as listening", () => {
  tile(A("f", "listening", { working: "open pull requests" }));
  expect(screen.getByText("Working: open pull requests")).toBeInTheDocument();
  expect(screen.queryByText("Say an agent's name to ask", { selector: "p.text-4xl" })).not.toBeInTheDocument();
});

it("lists hand-ups before working agents", () => {
  const { container } = tile({ ...A("a", "working", { working: "task" }), ...A("b", "has_news", { hand: "news" }) });
  const rows = [...container.querySelectorAll("ul")[0].querySelectorAll("li")].map((li) => li.lastElementChild?.textContent);
  expect(rows).toEqual(["Hand up: news", "Working: task"]);
});

it("collapses activity past three rows into +N more", () => {
  tile(Object.assign({}, ...["a", "b", "c", "d", "e"].map((id) => A(id, "has_news", { hand: id }))));
  expect(screen.getByText("+2 more")).toBeInTheDocument();
});

it("shows the calm line when nothing is going on, and never says tap", () => {
  const { container } = tile(A("a", "idle"));
  expect(screen.getByText("Say an agent's name to ask")).toBeInTheDocument();
  expect(container.textContent).not.toMatch(/tap/i);
});

it("caps the roster at eight with +N", () => {
  tile(Object.assign({}, ...["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"].map((id) => A(id, "idle"))));
  expect(screen.getByText("+2")).toBeInTheDocument();
});
