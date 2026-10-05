// A session link's page shows a customer's conversation and carries a link token: like /chat,
// session replay blocks it and autocapture skips it (PostHogProvider treats /s as private too).
export default function SessionLinkLayout({ children }: { children: React.ReactNode }) {
  return <div data-private className="contents ph-no-capture">{children}</div>;
}
