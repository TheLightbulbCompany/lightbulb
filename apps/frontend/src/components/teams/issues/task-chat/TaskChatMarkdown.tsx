// apps/frontend/src/components/teams/issues/task-chat/TaskChatMarkdown.tsx

// Stand-in for upstream Paperclip's MarkdownBody inside the task chat
// (paperclip/ui/src/components/MarkdownBody.tsx @ v2026.916.1-isol8.2, MIT,
// (c) 2025 Paperclip AI), built on our shared MarkdownView. Carries the one
// MarkdownBody behaviour the thread needs beyond it: `agent://` / `user://`
// mention links (upstream lib/mention-chips.ts) render as inert "@Name" chips
// instead of being blanked by react-markdown's default URL sanitizer.

"use client";

import type { ComponentProps, ComponentType } from "react";
import { defaultUrlTransform, type Components, type UrlTransform } from "react-markdown";
import {
  MarkdownView,
  markdownComponents,
} from "@/components/teams/shared/components/MarkdownView";

const MENTION_RE = /^(agent|user):\/\//;

const mentionUrlTransform: UrlTransform = (value) =>
  MENTION_RE.test(value) ? value : defaultUrlTransform(value);

const BaseLink = markdownComponents.a as ComponentType<ComponentProps<"a">>;

const COMPONENTS: Partial<Components> = {
  a: (props) => {
    if (props.href && MENTION_RE.test(props.href)) {
      return (
        <span
          className="rounded-sm px-1 font-medium"
          style={{ background: "var(--cream-deeper)", color: "var(--midnight)" }}
          data-mention-href={props.href}
        >
          {props.children}
        </span>
      );
    }
    return <BaseLink {...(props as ComponentProps<"a">)} />;
  },
};

export function TaskChatMarkdown({ children }: { children: string }) {
  return (
    <MarkdownView skipHtml components={COMPONENTS} urlTransform={mentionUrlTransform}>
      {children}
    </MarkdownView>
  );
}
