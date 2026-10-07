export type ShapeName = "scatter" | "brain" | "network" | "town" | "growth" | "gear";

/** Page order. Each chapter's shape is fully formed while its section fills the viewport. */
export const CHAPTERS = [
  { id: "hero", shape: "scatter" },
  { id: "strike", shape: "brain" },
  { id: "ai", shape: "network" },
  { id: "goose", shape: "town" },
  { id: "physical", shape: "gear" },
  { id: "ventures", shape: "growth" },
  { id: "vision", shape: "brain" },
] as const satisfies ReadonlyArray<{ id: string; shape: ShapeName }>;

/**
 * The products, in page order — a maturity gradient, and honest about it. The
 * `stage` is the bulb's own vocabulary for how far along an idea is; `idea`
 * marks the ones that are not products yet, which the page renders lighter.
 */
export const PRODUCTS = [
  {
    id: "ai",
    index: "02",
    stage: "Lit",
    note: "live",
    name: "Lightbulb AI",
    sub: "a team of agents — lit",
    href: "https://flickmeon.ai",
    blurb:
      "An AI company in a box. Sign up and you get a team of agents — a CEO, a recruiter, and whoever they hire next — working for you around the clock. Our first product, and the one that pays for the rest.",
  },
  {
    id: "goose",
    index: "03",
    stage: "Warming up",
    note: "in development",
    name: "GooseTown",
    sub: "a town of agents — warming up",
    blurb:
      "A town run entirely by AI agents: a place to watch what a society of minds does when nobody is typing. Part experiment, part entertainment. We're still building it, and it still surprises us.",
  },
  {
    id: "physical",
    index: "04",
    stage: "A spark",
    note: "an idea, honestly",
    name: "Physical AI",
    sub: "machines — a spark",
    idea: true,
    blurb:
      "Ideas that leave the screen: machines you can hand a task and trust. We haven't started. It's on this page because we want to be held to it.",
  },
  {
    id: "ventures",
    index: "05",
    stage: "Sketched",
    note: "not yet",
    name: "Ventures",
    sub: "a fund — sketched",
    idea: true,
    blurb:
      "Eventually, a fund. Small cheques and real help for founders lighting rooms of their own — once we've earned the right to. That's a few chapters away.",
  },
] as const;
