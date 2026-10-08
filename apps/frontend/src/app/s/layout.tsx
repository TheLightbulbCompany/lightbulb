// A session link's page shows a customer's conversation and carries a link token: like /chat,
// session replay blocks it and autocapture skips it (PostHogProvider treats /s as private too).
import type { Metadata } from "next";
import { ThemeStamp } from "@/components/ThemeStamp";

// How a pasted session link unfurls (iMessage, Slack). The same for every link: the token is in the
// fragment, which never reaches a server, so a preview cannot show a title or an owner. The image
// is opengraph-image.tsx beside this file.
const TITLE = "Shared session · Lightbulb";
const DESCRIPTION = "Open to watch this coding session live in a terminal.";
export const metadata: Metadata = {
  title: TITLE,
  description: DESCRIPTION,
  openGraph: { title: TITLE, description: DESCRIPTION, siteName: "Lightbulb", type: "website", url: "/s" },
  twitter: { card: "summary_large_image", title: TITLE, description: DESCRIPTION },
};

export default function SessionLinkLayout({ children }: { children: React.ReactNode }) {
  // Stamped like /chat: the page is part of the product, so it takes the app's neutral palette.
  return <><ThemeStamp /><div data-private className="contents ph-no-capture">{children}</div></>;
}
