import type { Metadata } from "next";
import Link from "next/link";

import { Logo } from "@/components/Logo";

export const metadata: Metadata = {
  title: "Data deletion — isol8",
  description:
    "What happens when you remove isol8 from a connected account, and how to request deletion of your data.",
};

/**
 * The status URL returned by the Meta data-deletion callback
 * (`routers/meta_webhooks.py`). Meta requires that URL to resolve and explain
 * the state of the request, so this page must stay reachable and must keep
 * describing deletion as ALREADY DONE — the callback tears the connection down
 * synchronously before it ever hands out a confirmation code.
 *
 * Static by design: we deliberately store no record of a deletion request, so
 * there is no per-code status to look up. A page that asked for a code and then
 * couldn't say anything specific would be worse than one that states the
 * outcome plainly.
 */
export default function DataDeletionPage() {
  return (
    <div className="legal-page">
      <header className="legal-topbar">
        <Logo size="md" />
        <Link href="/" className="legal-btn-back">
          Home
        </Link>
      </header>

      <hr className="legal-divider" />

      <main className="support-content">
        <div className="support-header">
          <h1>Data deletion</h1>
          <p>
            If you were given a confirmation code, the deletion has already completed — there is
            nothing left to wait for.
          </p>
        </div>

        <div className="support-form">
          <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
            Removing isol8 from a connected account
          </h2>
          <p style={{ marginBottom: "1rem", color: "var(--muted, #666)" }}>
            When you remove isol8 from an account you connected (for example, from your Facebook or
            Instagram settings), that provider notifies us and we immediately revoke our copy of the
            access it granted: the stored credential is deleted, and the connection is removed from
            your agent&rsquo;s container. Your agents lose access to that account right away.
          </p>

          <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
            Deleting a connection yourself
          </h2>
          <p style={{ marginBottom: "1rem", color: "var(--muted, #666)" }}>
            You can disconnect any integration at any time from the Integrations tab in the Control
            Panel, which performs the same revocation.
          </p>

          <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
            Deleting your isol8 account
          </h2>
          <p style={{ marginBottom: "1rem", color: "var(--muted, #666)" }}>
            To delete your isol8 account and everything associated with it, email{" "}
            <a href="mailto:support@isol8.co">support@isol8.co</a> from the address on the account.
            See our <Link href="/privacy">Privacy Policy</Link> for what we retain and for how long.
          </p>
        </div>
      </main>
    </div>
  );
}
