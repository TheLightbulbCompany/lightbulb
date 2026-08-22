import type { BlogPost } from "./types";
import { persistentAiAgentWorkspace } from "./posts/persistent-ai-agent-workspace";
import { connectAiAgentChannels } from "./posts/connect-ai-agent-slack-discord-telegram";
import { webhookEventReference } from "./posts/webhook-event-reference";

/**
 * Registry of all blog posts. Add new posts here.
 * `getAllPosts()` returns them newest-first.
 */
const registry: BlogPost[] = [
  webhookEventReference,
  persistentAiAgentWorkspace,
  connectAiAgentChannels,
];

export function getAllPosts(): BlogPost[] {
  return [...registry].sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
}

export function getPostBySlug(slug: string): BlogPost | undefined {
  return registry.find((p) => p.slug === slug);
}

export type { BlogPost };
