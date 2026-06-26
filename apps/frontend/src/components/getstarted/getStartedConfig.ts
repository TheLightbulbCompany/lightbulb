export type StepId = "meet" | "chat" | "task" | "hire" | "channel";

export interface StepDef {
  id: StepId;
  title: string;
  description: string;
  /** Empty for "meet" (it opens the article instead of a CTA). */
  ctaLabel: string;
  /** lucide-react icon name resolved by the component. */
  icon: "users" | "message" | "listplus" | "store" | "plug";
}

// Five core steps, in display order. "meet" is reading-based (opens its
// article); the rest are actions that minimize the card and hand off.
export const STEPS: StepDef[] = [
  { id: "meet", title: "Meet Friday & Atlas", description: "Your CEO and your recruiter — the two agents every workspace starts with.", ctaLabel: "", icon: "users" },
  { id: "chat", title: "Chat with Friday", description: "Talk to your CEO. Ask for something real — Friday plans it and delegates.", ctaLabel: "Open chat", icon: "message" },
  { id: "task", title: "Create your first task", description: "Open Tasks and file a job the team picks up and tracks.", ctaLabel: "New task", icon: "listplus" },
  { id: "hire", title: "Hire your first specialist", description: "Deploy a ready-made agent into your team from the marketplace.", ctaLabel: "Browse marketplace", icon: "store" },
  { id: "channel", title: "Connect a channel", description: "Reach your agents from Slack, Discord, Telegram or WhatsApp.", ctaLabel: "Set up channels", icon: "plug" },
];
