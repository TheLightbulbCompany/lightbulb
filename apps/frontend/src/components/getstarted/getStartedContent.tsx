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

function StarterTeamDiagram(): React.ReactElement {
  return (
    <div role="img" aria-label="The CEO delegates to Engineering, Finance, Growth, Analytics, and Customer Operations.">
      <div className="mx-auto mb-3 w-fit rounded-lg bg-[var(--midnight)] px-5 py-2 text-sm font-bold text-[var(--cream)]">CEO</div>
      <div className="grid grid-cols-2 gap-2 text-xs font-semibold text-[var(--midnight)] sm:grid-cols-3">
        {["Engineering", "Finance", "Growth", "Analytics", "Customer Operations"].map((role) => (
          <div key={role} className="rounded-lg border border-[var(--brass)] bg-[var(--cream)] px-2 py-2">{role}</div>
        ))}
      </div>
    </div>
  );
}

const P = "text-sm leading-relaxed text-[var(--midnight)] mb-3";

export const ARTICLES: Article[] = [
  {
    id: "meet-friday-atlas", tag: "The agents", title: "Meet your starter team",
    body: (<>
      <p className={P}>Every new workspace starts with six role-based agents. Talk to <b>your CEO</b>; it plans, delegates to the functional team, and reports back.</p>
      <div className="bg-[var(--cream-deep)] border border-[var(--edge)] rounded-xl p-4 text-center mb-4"><StarterTeamDiagram /></div>
      <p className={P}><b>Engineering</b> builds, <b>Finance</b> tracks margin and runway, <b>Growth</b> owns distribution, <b>Analytics</b> measures results, and <b>Customer Operations</b> supports customers.</p>
      <p className={P}>These are roles, not permanent names. Rename any agent to fit your company; its identity and policies stay attached to the same agent.</p>
    </>),
  },
  {
    id: "getting-around", tag: "Orientation", title: "Getting around",
    body: (<>
      <p className={P}>Everything happens in one workspace. The left sidebar is your map.</p>
      <p className={P}><b>My agents</b> — your six-role starter roster plus any agents you hire. Click one to chat.</p>
      <p className={P}><b>Company panels</b> — Dashboard, Inbox, Tasks, Automations, Goals, Projects, Org chart, Skills, Activity: how the team’s work is tracked.</p>
      <p className={P}><b>Marketplace · Browse / Publish</b> — hire ready-made agents, or publish your own.</p>
      <p className={P}><b>Settings</b> (the gear) — Profile, Billing, Channels, Marketplace, Skills, Connections.</p>
    </>),
  },
  { id: "company-panels", tag: "The work", title: "Company panels",
    body: (<><p className={P}>The Company section is where your team’s work lives. <b>Inbox</b> and <b>Tasks</b> hold the jobs agents pick up; <b>Automations</b> and <b>Goals</b> set recurring and longer-horizon work; <b>Org chart</b>, <b>Skills</b> and <b>Activity</b> show how the team is shaped and what it’s been doing.</p></>) },
  { id: "marketplace", tag: "Grow your team", title: "The marketplace",
    body: (<><p className={P}>Open <b>Browse</b> to hire a ready-made specialist — including the optional Friday and Atlas products — straight into your roster. Open <b>Publish</b> to package one of your own agents and share it with the community. Your workspace&rsquo;s root CEO cannot be published.</p></>) },
  { id: "channels", tag: "Reach", title: "Channels",
    body: (<><p className={P}>Connect Slack, Discord or Telegram to talk to your agents from the apps you already use. Set one up from an agent&rsquo;s <b>Channels</b> tab; each agent can have its own bot.</p></>) },
  { id: "billing-providers", tag: "Reference", title: "Billing & AI providers",
    body: (<><p className={P}>New accounts start on the <b>free tier</b>: 3 days, no card, with up to $3 of Claude a day. When you&rsquo;re ready to keep going there are three ways to power inference: managed <b>Claude credits</b>, your own <b>ChatGPT</b> account, or your own <b>API key</b>. Manage all of it in <b>Settings → Billing</b>.</p></>) },
];
