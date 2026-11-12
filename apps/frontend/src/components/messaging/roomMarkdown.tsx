import type { ReactNode } from "react";
import type { Components } from "react-markdown";
import { cardMarkdown } from "@/components/voice/cardMarkdown";
import { appLinkTarget, type AppLink } from "./appLinks";
import { useMessagingWorkspaceOptional } from "./MessagingWorkspaceContext";
import { openAppLink } from "./RoomAppLinks";

/** The voice thread's markdown (cards, diagrams, safe links), plus links into our own app opening in the shell. */
export const roomMarkdown: Partial<Components> = {
  ...cardMarkdown,
  a: (props) => {
    // Links into our own app — the bundled paperclip skill's native paths (`/ISOA/issues/ISOA-171`) and absolute
    // thelightbulb.company links — open in the shell, in place, instead of a new browser tab.
    const link = appLinkTarget(props.href);
    if (link && link.href.startsWith("/chat")) return <AppAnchor link={link}>{props.children}</AppAnchor>;
    const A = cardMarkdown.a as (p: typeof props) => ReactNode;
    return A(props);
  },
};

function AppAnchor({ link, children }: { link: AppLink; children: ReactNode }) {
  const ws = useMessagingWorkspaceOptional();
  return <a href={link.href} className="text-info underline underline-offset-4" onClick={(e) => openAppLink(e, link, ws)}>{children}</a>;
}
