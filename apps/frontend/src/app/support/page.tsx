"use client";

import Link from "next/link";

import { capture } from "@/lib/analytics";
import { SUPPORT_EMAIL, SUPPORT_MAILTO } from "@/lib/domain";
import { BackButton } from "@/components/legal/BackButton";
import { Logo } from "@/components/Logo";

export default function SupportPage() {
  return (
    <div className="legal-page">
      <header className="legal-topbar">
        <Logo size="md" />
        <BackButton />
      </header>

      <hr className="legal-divider" />

      <main className="support-content">
        <div className="support-header">
          <h1>Support</h1>
          <p>Email us and we&rsquo;ll reply.</p>
        </div>

        <div className="support-direct">
          <p>Write to us at <a
            href={SUPPORT_MAILTO}
            onClick={() => capture("support_contact_clicked", { source: "support_page" })}
          >{SUPPORT_EMAIL}</a></p>
        </div>

        {/* Declared as the data deletion instructions URL for the Meta app
            (App settings -> Basic). Reviewers open this section directly. */}
        <div className="support-direct" id="data-deletion">
          <h2>Deleting your data</h2>
          <p>
            To delete your Lightbulb account and everything we hold for it &mdash; your assistant,
            its calls, transcripts and bookings, and the credentials for any service you connected
            &mdash; email <a
              href={`${SUPPORT_MAILTO}?subject=Delete%20my%20account`}
              onClick={() => capture("support_contact_clicked", { source: "support_page_data_deletion" })}
            >{SUPPORT_EMAIL}</a> from your account email address and we&rsquo;ll action it.
          </p>
          <p>
            On a personal account you can also delete it yourself in the Lightbulb iOS app, under
            Settings. Team accounts are deleted on request.
          </p>
          <p>
            To remove just one connected service, disconnect it in the Integrations tab of the
            Control Panel &mdash; that revokes our access and deletes the credentials we stored
            for it, leaving the rest of your account intact. Account deletion is irreversible.
          </p>
        </div>
      </main>

      <footer className="legal-footer">
        <p>&copy; {new Date().getFullYear()} Isol8 Inc., doing business as Lightbulb AI. All rights reserved.</p>
        <nav className="legal-footer-links">
          <Link href="/privacy">Privacy Policy</Link>
          <Link href="/terms">Terms</Link>
        </nav>
      </footer>
    </div>
  );
}
