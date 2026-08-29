import React from "react";

export type ArticleId =
  | "meet-friday-atlas" | "getting-around" | "company-panels"
  | "marketplace" | "channels" | "billing-providers";

export interface Article {
  id: ArticleId;
  tag: string;
  title: string;
  /** Rendered inside the article reader. */
  body: React.ReactNode;
}

// Friday → Atlas → specialists org-chart, ported from the approved mockup.
export function FridayAtlasDiagram(): React.ReactElement {
  return (
    <svg width="300" height="186" viewBox="0 0 300 186" fill="none"
      role="img" aria-label="Friday delegates to Atlas and to specialists; Atlas recruits the specialists.">
      <path d="M150 52 V72 M150 72 H78 M150 72 H222 M78 72 V92 M222 72 V92" stroke="var(--midnight-faint)" strokeWidth="1.6" />
      <path d="M222 92 V108 M222 108 H190 M222 108 H254 M190 108 V120 M254 108 V120" stroke="var(--midnight-faint)" strokeWidth="1.4" />
      <path d="M96 132 C140 150 178 140 198 124" stroke="var(--brass)" strokeWidth="1.6" strokeDasharray="4 4" markerEnd="url(#gsArrow)" />
      <defs><marker id="gsArrow" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7 Z" fill="var(--brass)" /></marker></defs>
      <rect x="112" y="22" width="76" height="32" rx="9" fill="var(--midnight)" />
      <text x="150" y="42" textAnchor="middle" fontSize="13" fontWeight="700" fill="var(--cream)">Friday</text>
      <text x="150" y="13" textAnchor="middle" fontSize="9" fill="var(--midnight-muted)">CEO</text>
      <rect x="44" y="92" width="68" height="30" rx="8" fill="var(--brass)" />
      <text x="78" y="111" textAnchor="middle" fontSize="12" fontWeight="700" fill="var(--midnight)">Atlas</text>
      <rect x="170" y="120" width="40" height="26" rx="7" fill="var(--cream)" stroke="var(--brass)" strokeWidth="1.4" />
      <text x="190" y="136" textAnchor="middle" fontSize="9" fontWeight="600" fill="var(--midnight)">Sales</text>
      <rect x="234" y="120" width="40" height="26" rx="7" fill="var(--cream)" stroke="var(--brass)" strokeWidth="1.4" />
      <text x="254" y="136" textAnchor="middle" fontSize="9" fontWeight="600" fill="var(--midnight)">Research</text>
      <text x="222" y="170" textAnchor="middle" fontSize="9" fill="var(--midnight-muted)">specialists you hire</text>
    </svg>
  );
}

const P = "text-sm leading-relaxed text-[var(--midnight)] mb-3";

export const ARTICLES: Article[] = [
  {
    id: "meet-friday-atlas", tag: "The agents", title: "Meet Friday & Atlas",
    body: (<>
      <p className={P}>Lightbulb works like a tiny company that runs itself. You don’t wire up agents one by one — you tell <b>Friday</b> what you want, and Friday assembles the team to do it.</p>
      <div className="bg-[var(--cream-deep)] border border-[var(--edge)] rounded-xl p-4 text-center mb-4"><FridayAtlasDiagram /></div>
      <p className={P}><b>Friday — your CEO.</b> The orchestrator you talk to. Give it a goal; it plans, delegates to specialists, and reports back.</p>
      <p className={P}><b>Atlas — your recruiter.</b> When a job needs a skill you don’t have yet, Atlas finds or builds the right agent from the marketplace.</p>
    </>),
  },
  {
    id: "getting-around", tag: "Orientation", title: "Getting around",
    body: (<>
      <p className={P}>Everything happens in one workspace. The left sidebar is your map.</p>
      <p className={P}><b>My agents</b> — your roster (Friday, Atlas, hired specialists). Click one to chat.</p>
      <p className={P}><b>Company panels</b> — Dashboard, Inbox, Tasks, Automations, Goals, Projects, Org chart, Skills, Activity: how the team’s work is tracked.</p>
      <p className={P}><b>Marketplace · Browse / Publish</b> — hire ready-made agents, or publish your own.</p>
      <p className={P}><b>Settings</b> (the gear) — Profile, Billing, Channels, Marketplace, Skills, Connections.</p>
    </>),
  },
  { id: "company-panels", tag: "The work", title: "Company panels",
    body: (<><p className={P}>The Company section is where your team’s work lives. <b>Inbox</b> and <b>Tasks</b> hold the jobs agents pick up; <b>Automations</b> and <b>Goals</b> set recurring and longer-horizon work; <b>Org chart</b>, <b>Skills</b> and <b>Activity</b> show how the team is shaped and what it’s been doing.</p></>) },
  { id: "marketplace", tag: "Grow your team", title: "The marketplace",
    body: (<><p className={P}>Open <b>Browse</b> to hire a ready-made specialist — it’s deployed straight into your roster. Open <b>Publish</b> to package one of your own agents and share it with the community. Friday and Atlas can’t be published; everything else can.</p></>) },
  { id: "channels", tag: "Reach", title: "Channels",
    body: (<><p className={P}>Connect Slack, Discord or Telegram to talk to your agents from the apps you already use. Set one up from an agent&rsquo;s <b>Channels</b> tab; each agent can have its own bot.</p></>) },
  { id: "billing-providers", tag: "Reference", title: "Billing & AI providers",
    body: (<><p className={P}>Your plan and AI provider were chosen at signup. There are three ways to power inference: managed <b>Claude credits</b>, your own <b>ChatGPT</b> account, or your own <b>API key</b>. Manage all of it in <b>Settings → Billing</b>.</p></>) },
];
