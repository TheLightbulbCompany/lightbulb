// A session link's page shows a customer's conversation and carries a link token: like /chat,
// session replay blocks it and autocapture skips it (PostHogProvider treats /s as private too).
import { ThemeStamp } from "@/components/ThemeStamp";

export default function SessionLinkLayout({ children }: { children: React.ReactNode }) {
  // Stamped like /chat: the page is part of the product, so it takes the app's neutral palette.
  return <><ThemeStamp /><div data-private className="contents ph-no-capture">{children}</div></>;
}
