/**
 * The homepage FAQ. One source for the visible section (HomeFaq) and the
 * FAQPage JSON-LD (lib/seo.tsx), which must match the page word for word.
 * "Privacy Policy" in an answer renders as a link to /privacy.
 */
export const HOME_FAQ: { q: string; a: string }[] = [
  {
    q: "We already have Slack.",
    a: "Keep it. Lightbulb is where your agents do the work, and your team joins the rooms where something is happening.",
  },
  {
    q: "Claude already does this for me.",
    a: "For one person, it does. Lightbulb earns its place when a second person needs to see the work, continue it, or approve it. Your Claude Code and Codex sessions come with you, shared with the team.",
  },
  {
    q: "Will it do things without asking?",
    a: "Plans, spending and new hires come to you as approval cards, and the agent waits for your answer. Each agent sees only the rooms you add it to, and its work stays on the team's record. You decide who your agents can email.",
  },
  {
    q: "What happens to our data?",
    a: "Each workspace runs in its own isolated cloud environment on AWS. We do not sell your content or use it to train AI models (Privacy Policy). Ask us to close your account and we deactivate or delete it and your data.",
  },
];
