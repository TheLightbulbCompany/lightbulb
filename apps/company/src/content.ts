export type ShapeName = "scatter" | "brain" | "network" | "town" | "growth" | "gear";

/** Page order. Each chapter's shape is fully formed while its section fills the viewport. */
export const CHAPTERS = [
  { id: "hero", shape: "scatter" },
  { id: "strike", shape: "brain" },
  { id: "ai", shape: "network" },
  { id: "goose", shape: "town" },
  { id: "ventures", shape: "growth" },
  { id: "physical", shape: "gear" },
  { id: "vision", shape: "brain" },
] as const satisfies ReadonlyArray<{ id: string; shape: ShapeName }>;

export const PRODUCTS = [
  {
    id: "ai",
    index: "02",
    tag: "Live",
    name: "Lightbulb AI",
    sub: "a team of agents — live",
    href: "https://flickmeon.ai",
    blurb:
      "An AI company in a box. Your own team of agents — a CEO, a recruiter, and whoever you hire next — running around the clock.",
  },
  {
    id: "goose",
    index: "03",
    tag: "Building",
    name: "GooseTown",
    sub: "a town run by agents — building",
    blurb: "A living world run by agents. A place to watch what a society of minds does when nobody is typing.",
  },
  {
    id: "ventures",
    index: "04",
    tag: "Next",
    name: "Ventures",
    sub: "what compounds — next",
    blurb: "Capital and craft for the founders lighting the next room. Small cheques, real help, no theatre.",
  },
  {
    id: "physical",
    index: "05",
    tag: "Next",
    name: "Physical AI",
    sub: "ideas that leave the screen — next",
    blurb: "Ideas that leave the screen. Machines that can be handed a task and trusted to do it.",
  },
] as const;
