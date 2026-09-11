/**
 * Isol8 marketplace tools — READ-ONLY.
 *
 * Neither tool mutates the roster, EFS, or openclaw.json. That is structural,
 * not a convention: `tools.exec.mode` is "full" fleet-wide (approvals are off,
 * because autonomous Paperclip runs have no operator to answer one), so a write
 * tool gated on an approval would hang until the 120s gateway timeout and then
 * be re-dispatched forever. Hiring therefore always ends at a human click —
 * the hire card in chat, or a Paperclip `hire_agent` approval in Teams.
 *
 * The plugin is deliberately dumb: a typed shell over HTTP. Ranking,
 * projection, and policy live in the backend so they can change without an
 * image rebuild and a dev -> prod tag promotion.
 */
import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { buildRecommendPath, buildSearchPath, resolveEnv } from "./env.js";

const LISTING = Type.Object(
  {
    // The canonical identity. Slugs are NOT unique, so every downstream
    // action (deploy, checkout) addresses the listing by this UUID.
    listing_id: Type.String(),
    slug: Type.String(),
    name: Type.String(),
    summary: Type.String(),
    tags: Type.Array(Type.String()),
    price_cents: Type.Number(),
  },
  { additionalProperties: false },
);

async function getJson(path: string, signal?: AbortSignal): Promise<any> {
  const { base, token } = resolveEnv(process.env);
  const res = await fetch(`${base}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  if (!res.ok) {
    throw new Error(`Lightbulb API ${res.status}: ${(await res.text()).slice(0, 300)}`);
  }
  return res.json();
}

export default defineToolPlugin({
  id: "lightbulb-marketplace",
  name: "Lightbulb Marketplace",
  description: "Discover hireable agents in the Lightbulb marketplace and recommend them to the owner.",
  tools: (tool) => [
    tool({
      name: "marketplace_search",
      label: "Search the Lightbulb marketplace",
      description:
        "Search published Lightbulb marketplace agents the owner could HIRE onto their team. " +
        "This is a staffing tool, not a way to get work done. Call it when hiring is the " +
        "actual question: the owner asks who could take something on, asks to hire or add " +
        "an agent, or the team is short a role and that gap is what is blocking the work. " +
        "Do NOT call it to carry out a request — a task you or a teammate can already do " +
        "with the tools you have is not a hiring question, however specialised it sounds. " +
        "When hiring IS the question, search here BEFORE asking Atlas to build an agent: an " +
        "existing listing is ready immediately, and free ones cost nothing to add.",
      parameters: Type.Object({
        query: Type.String({
          description: "What the work needs, e.g. 'content marketing' or 'bookkeeping'.",
        }),
      }),
      outputSchema: Type.Array(LISTING),
      async execute({ query }, _config, context) {
        context.signal?.throwIfAborted();
        const data = await getJson(buildSearchPath(query), context.signal);
        return data.items ?? [];
      },
    }),
    tool({
      name: "recommend_agents",
      label: "Recommend agents to add to the team",
      description:
        "Recommend marketplace agents to the owner. In chat this renders a one-click hire card. " +
        "You cannot add an agent yourself — the owner decides. " +
        "When working a Paperclip task, pass your PAPERCLIP_TASK_ID as issue_id so the request " +
        "reaches them on that task in Teams instead of a chat nobody is reading. " +
        "Name the agents in your reply too, so the recommendation still reads where no card renders.",
      parameters: Type.Object({
        slugs: Type.Array(Type.String(), {
          description: "Listing slugs returned by marketplace_search.",
        }),
        reason: Type.String({ description: "One line on why these fit the work." }),
        issue_id: Type.Optional(
          Type.String({
            description: "The Paperclip issue you are working (PAPERCLIP_TASK_ID). Omit in webchat.",
          }),
        ),
      }),
      outputSchema: Type.Object(
        {
          kind: Type.Literal("isol8.hire_suggestion"),
          reason: Type.String(),
          listings: Type.Array(LISTING),
          approval_id: Type.Union([Type.String(), Type.Null()]),
        },
        { additionalProperties: false },
      ),
      async execute({ slugs, reason, issue_id }, _config, context) {
        context.signal?.throwIfAborted();
        const { ownerId } = resolveEnv(process.env);
        const data = await getJson(
          buildRecommendPath(ownerId, slugs, issue_id),
          context.signal,
        );
        return {
          kind: "isol8.hire_suggestion" as const,
          reason,
          listings: data.listings ?? [],
          approval_id: data.approval_id ?? null,
        };
      },
    }),
  ],
});
