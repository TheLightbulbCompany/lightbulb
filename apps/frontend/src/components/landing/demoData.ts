// Canned data for the landing page's WorkspaceDemo — a ceramics-studio story
// told through the REAL /chat component types (Issue, CompanyAgent,
// IssueComment from the Teams surface; RenderGroup from the chat surface), so
// the demo renders through the same components the product does.
//
// Timestamps are offsets from module-load time so "Updated 2h ago" never rots.
// The offsets are chosen to exercise both inbox sections: the research task
// landed >24h ago (Earlier), everything else is Today.

import type {
  CompanyAgent,
  Issue,
  IssueComment,
} from "@/components/teams/shared/types";
import type { RenderGroup } from "@/hooks/internal/types";

const NOW = Date.now();
const MIN = 60_000;
const minutesAgo = (n: number) => new Date(NOW - n * MIN).toISOString();

/** Passed to buildGroupedInboxSections so Today/Earlier split off the same clock. */
export const DEMO_NOW_ISO = new Date(NOW).toISOString();

export const FRIDAY_ID = "agent-friday";
export const ATLAS_ID = "agent-atlas";
export const STITCH_ID = "agent-stitch";
export const TALLY_ID = "agent-tally";

export const DEMO_COMPANY_NAME = "Northwind Ceramics";

/** Model subtitle shown under each roster row + on the composer chip. */
export const DEMO_MODEL_NAME = "Claude Sonnet 4.6";

/**
 * Fixed pastel identities for the roster monograms (the real sidebar hashes
 * agent ids through lib/agentDesign's PALETTE; canned ids hash to whatever,
 * so the demo pins the swatches instead — same palette entries).
 */
export const DEMO_AGENT_LOOKS: Record<
  string,
  { monogram: string; accent: string; tint: string }
> = {
  [FRIDAY_ID]: { monogram: "Fr", accent: "#3a4a8a", tint: "#e8eaf3" },
  [ATLAS_ID]: { monogram: "At", accent: "#2d6b4e", tint: "#e3eee7" },
  [STITCH_ID]: { monogram: "St", accent: "#a35d1f", tint: "#f3ebe0" },
  [TALLY_ID]: { monogram: "Ta", accent: "#7a3a5a", tint: "#f1e6ec" },
};

export const DEMO_AGENTS: CompanyAgent[] = [
  {
    id: FRIDAY_ID,
    name: "Friday",
    role: "ceo",
    title: "CEO · runs the company",
    status: "running",
    seeded: true,
  },
  {
    id: ATLAS_ID,
    name: "Atlas",
    role: "recruiter",
    title: "Recruiter · hires specialists",
    status: "idle",
    reportsTo: FRIDAY_ID,
    seeded: true,
  },
  {
    id: STITCH_ID,
    name: "Stitch",
    role: "growth",
    title: "Growth · outreach & sales",
    status: "idle",
    reportsTo: FRIDAY_ID,
  },
  {
    id: TALLY_ID,
    name: "Tally",
    role: "finance",
    title: "Finance · pricing & books",
    status: "idle",
    reportsTo: FRIDAY_ID,
  },
];

const LAUNCH_PROJECT = { id: "proj-launch", name: "Launch" };

export const DEMO_ISSUES: Issue[] = [
  {
    id: "iss-research",
    identifier: "NWC-1",
    title: "Research the market",
    description:
      "Size the direct-to-consumer ceramics market, map the strongest " +
      "competitors, and find the underserved channel we should enter through.",
    status: "done",
    priority: "high",
    labels: [{ id: "lbl-research", name: "research" }],
    project: LAUNCH_PROJECT,
    assigneeAgentId: FRIDAY_ID,
    createdAt: minutesAgo(60 * 30),
    updatedAt: minutesAgo(60 * 26),
    lastActivityAt: minutesAgo(60 * 26),
    unread: false,
  },
  {
    id: "iss-name",
    identifier: "NWC-2",
    title: "Name it and take the address",
    description:
      "Settle the studio name. It becomes the company's email subdomain, so " +
      "the name we pick is the address we keep — it can't be changed later.",
    status: "in_progress",
    priority: "high",
    labels: [{ id: "lbl-brand", name: "brand" }],
    project: LAUNCH_PROJECT,
    assigneeAgentId: FRIDAY_ID,
    createdAt: minutesAgo(60 * 26),
    updatedAt: minutesAgo(14),
    lastActivityAt: minutesAgo(14),
    unread: true,
  },
  {
    id: "iss-buyers",
    identifier: "NWC-3",
    title: "Find the first fifty buyers",
    description:
      "Build the outreach list — interior designers and small hotels first — " +
      "and draft the opening note for each segment.",
    status: "todo",
    priority: "medium",
    labels: [{ id: "lbl-sales", name: "sales" }],
    project: LAUNCH_PROJECT,
    assigneeAgentId: STITCH_ID,
    createdAt: minutesAgo(60 * 26),
    updatedAt: minutesAgo(60 * 2),
    lastActivityAt: minutesAgo(60 * 2),
    unread: false,
  },
  {
    id: "iss-pricing",
    identifier: "NWC-4",
    title: "Price it against the market",
    description:
      "Land the price ladder: per-piece retail, the hospitality wholesale " +
      "tier, and where free shipping stops hurting margin.",
    status: "todo",
    priority: "medium",
    labels: [{ id: "lbl-pricing", name: "pricing" }],
    project: LAUNCH_PROJECT,
    assigneeAgentId: TALLY_ID,
    createdAt: minutesAgo(60 * 26),
    updatedAt: minutesAgo(60 * 5),
    lastActivityAt: minutesAgo(60 * 5),
    unread: false,
  },
];

/** Issues with an active agent run — drives the pulsing live dot. */
export const DEMO_LIVE_ISSUE_IDS = new Set<string>(["iss-name"]);

export const DEMO_COMMENTS: Record<string, IssueComment[]> = {
  "iss-research": [
    {
      id: "cmt-research-1",
      body:
        "Research is in. DTC ceramics is growing ~14% a year, but the crowded " +
        "lane is Etsy-style retail. The open lane is **wholesale to small " +
        "hospitality** — boutique hotels reorder, and almost nobody courts " +
        "them directly. Full brief attached to this task.",
      createdAt: minutesAgo(60 * 26),
      authorAgentId: FRIDAY_ID,
      authorKind: "agent",
    },
  ],
  "iss-name": [
    {
      id: "cmt-name-1",
      body:
        "Shortlist: **Northwind Ceramics**, Kiln & Coast, Second Firing. " +
        "Northwind wins on memorability and nothing else is trading under it, " +
        "so that's the name. Minting the address now — you'll reach me at " +
        "**friday@northwindceramics.isol8.co**, and the rest of the team gets " +
        "addresses on the same domain.",
      createdAt: minutesAgo(14),
      authorAgentId: FRIDAY_ID,
      authorKind: "agent",
    },
  ],
  "iss-buyers": [
    {
      id: "cmt-buyers-1",
      body:
        "Outreach list drafted — 32 interior design studios and 18 boutique " +
        "hotels within shipping range. Holding send until the name lands so " +
        "the first impression carries it.",
      createdAt: minutesAgo(60 * 2),
      authorAgentId: STITCH_ID,
      authorKind: "agent",
    },
  ],
  "iss-pricing": [
    {
      id: "cmt-pricing-1",
      body:
        "Comparable pieces retail $38–65. I'll model three ladders once the " +
        "positioning settles — premium-retail, hospitality-wholesale, and a " +
        "hybrid with a trade discount.",
      createdAt: minutesAgo(60 * 5),
      authorAgentId: TALLY_ID,
      authorKind: "agent",
    },
  ],
};

/**
 * Friday's kickoff conversation, in the exact RenderGroup shape MessageList
 * consumes: the visitor's brief, then Friday working — tool pills for the
 * research calls, then the plan and the board handoff.
 */
export const DEMO_RENDER_GROUPS: RenderGroup[] = [
  {
    side: "user",
    key: "grp-user-1",
    items: [
      {
        kind: "message",
        key: "msg-user-1",
        message: {
          role: "user",
          content:
            "A ceramics studio that ships nationwide — handmade tableware " +
            "for interior designers and small hotels.",
          timestamp: NOW - 32 * MIN,
        },
      },
    ],
  },
  {
    side: "assistant",
    key: "grp-friday-1",
    items: [
      {
        kind: "message",
        key: "msg-friday-1",
        message: {
          role: "assistant",
          timestamp: NOW - 31 * MIN,
          content: [
            {
              type: "text",
              text: "Love it. Give me a minute on the market before I lay out your first moves.",
            },
            {
              type: "tool_use",
              id: "tool-market",
              name: "web_search",
              input: { query: "handmade ceramics tableware DTC market 2026" },
            },
            {
              type: "tool_result",
              tool_use_id: "tool-market",
              content:
                "DTC ceramics up ~14% YoY. Retail marketplaces are saturated; " +
                "direct wholesale to boutique hospitality is the underserved " +
                "channel — repeat orders, low churn.",
            },
            {
              type: "tool_use",
              id: "tool-handle",
              name: "web_search",
              input: { query: "\"northwind ceramics\" existing brands + name collisions" },
            },
            {
              type: "tool_result",
              tool_use_id: "tool-handle",
              content:
                "No active ceramics brand trading as Northwind in the US or " +
                "EU. Clear to use.",
            },
            {
              type: "text",
              text:
                "Here's where I'd start:\n\n" +
                "1. **Name it and take the address** — I have a shortlist, and the name becomes your company's email domain. I'm on it now.\n" +
                "2. **Find the first fifty buyers** — designers and small hotels, warm outreach. Stitch takes this.\n" +
                "3. **Price it against the market** — retail ladder plus a wholesale tier. Tally takes this.\n\n" +
                "Market research is done and on the board — the full write-up is in your inbox. You'll see each task move as we work; anything you'd steer differently, say so.",
            },
          ],
        },
      },
    ],
  },
];
