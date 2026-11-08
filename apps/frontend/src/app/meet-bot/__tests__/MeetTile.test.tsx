import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { MeetTile, initialsOf } from "../MeetTile";

const A = (id: string, status: string, extra: Record<string, string> = {}) =>
  Object.fromEntries([[`lightbulb.agent.${id}.status`, status], ...Object.entries(extra).map(([k, v]) => [`lightbulb.agent.${id}.${k}`, v])]);
const tile = (attrs: Record<string, string>, agents: Record<string, { id: string; name: string }> = {}) =>
  render(<MeetTile agents={agents} attrs={attrs} notice={null} />);
const rosterNames = (c: HTMLElement) => [...c.querySelectorAll("[data-roster] [data-name]")].map((e) => e.textContent).join("");

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
  expect(container.querySelector("[data-speaker-name]")?.textContent).toBe("Forge");
});

it("shows Thinking, with no request text, for an agent that holds the floor as listening", () => {
  tile(A("f", "listening", { working: "open pull requests" }));
  expect(screen.getByText("Thinking")).toBeInTheDocument();
});

it("makes the first raised hand the hero, with no topic and no instruction", () => {
  const { container } = tile({ ...A("a", "working", { working: "task" }), ...A("b", "has_news", { hand: "news" }), ...A("c", "has_news", { hand: "more" }) });
  const hero = container.querySelector("[data-hand]")!;
  expect(hero.textContent).toBe("Bb has an answer"); // its initial, then the one line
  const rows = [...container.querySelectorAll("ul")[0].querySelectorAll("li")].map((li) => li.lastElementChild?.textContent);
  expect(rows).toEqual(["Hand up", "Thinking"]);
  expect(container.textContent).not.toMatch(/news|more|go ahead/i);
});

it("a speaker keeps the stage: a raised hand waits as a row", () => {
  const { container } = tile({ ...A("a", "speaking"), ...A("b", "has_news", { hand: "news" }) });
  expect(container.querySelector("[data-hand]")).toBeNull();
  expect(container.textContent).toContain("Hand up");
});

it("says Listening to <name> for an agent that heard its name", () => {
  const { container } = tile(A("a", "working", { working: "listening to Priya" }));
  expect(container.textContent).toContain("Listening to Priya");
  expect(container.textContent).not.toContain("Working:");
});

it("collapses activity past three rows into +N more", () => {
  tile(Object.assign({}, ...["a", "b", "c", "d", "e"].map((id) => A(id, "working", { working: id }))));
  expect(screen.getByText("+2 more")).toBeInTheDocument();
});

it("shows the calm line when nothing is going on, and never says tap", () => {
  const { container } = tile(A("a", "idle"));
  expect(screen.getByText("Say an agent's name to ask")).toBeInTheDocument();
  expect(container.textContent).not.toMatch(/tap/i);
});

const many = (n: number, status: string) => Object.assign({}, ...Array.from({ length: n }, (_, i) => A(`a${String(i).padStart(2, "0")}`, status)));

it("shows up to ten agents in the idle hero, then +N, with no bottom strip", () => {
  const { container } = tile(many(12, "idle"));
  expect(screen.getByText("+2")).toBeInTheDocument();
  expect(container.querySelectorAll("[data-roster]")).toHaveLength(1);
  expect(container.querySelectorAll("[data-roster] li")).toHaveLength(11);
});

it("caps the bottom strip at eight with +N when something is on stage", () => {
  tile({ ...many(10, "idle"), ...A("a00", "working", { working: "x" }) });
  expect(screen.getByText("+2")).toBeInTheDocument();
});

it("uses two-letter initials", () => {
  expect(initialsOf("Forge")).toBe("Fo");
  expect(initialsOf("friday")).toBe("Fr");
  expect(initialsOf("Gauge Prime")).toBe("GP");
});

it("uses the idle layout for a voice limit, with the limit text in place of the hint", () => {
  tile({ ...A("a", "idle"), "lightbulb.voice_limit": "credits_low" });
  expect(screen.getAllByRole("status")).toHaveLength(1);
  expect(screen.queryByText("Say an agent's name to ask")).not.toBeInTheDocument();
});

it("keeps Connecting until the worker's agents show up, so nobody is told to ask an empty room", () => {
  tile({});
  expect(screen.getByRole("status").textContent).toBe("Connecting…");
  expect(screen.queryByText("Say an agent's name to ask")).toBeNull();
});

it("keeps a long agent name on one line inside the tile", () => {
  const long = "Bartholomew the Extraordinarily Long-Named Engineering Agent";
  const { container } = tile({ ...A("f", "speaking"), "lightbulb.speaking_agent": "f", ...A("g", "working", { working: "x" }) }, { f: { id: "f", name: long }, g: { id: "g", name: long } });
  expect(container.querySelector("[data-speaker-name]")?.className).toMatch(/truncate/);
  expect(container.querySelector("[data-row-name]")?.className).toMatch(/truncate/);
});

it("draws everything inside the middle of the canvas, where no Meet tile crops it", () => {
  const { container } = tile(A("f", "idle"));
  const safe = container.querySelector<HTMLElement>("[data-safe]");
  expect(safe?.style.transform).toBe("scale(0.8)");
  expect(safe?.textContent).toContain("Lightbulb"); // the label is inside it too
});

it("MeetTile shows Drafting and Page ready", () => {
  const phase = (v: string) => ({ ...A("assistant", "listening", { name: "Nova" }), "lightbulb.agent.assistant.phase": v });
  const a = tile(phase("drafting"));
  expect(screen.getByText("Drafting…")).toBeInTheDocument();
  a.unmount();
  tile(phase("page_ready"));
  expect(screen.getByText("Page ready")).toBeInTheDocument();
});
