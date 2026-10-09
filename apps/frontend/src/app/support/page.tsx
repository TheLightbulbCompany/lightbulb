"use client";

import { useUser } from "@clerk/nextjs";
import Link from "next/link";

import { capture } from "@/lib/analytics";
import { SUPPORT_EMAIL, SUPPORT_MAILTO } from "@/lib/domain";
import { BackButton } from "@/components/legal/BackButton";
import { Logo } from "@/components/Logo";
import { useSupport } from "@/components/support/SupportProvider";
import { SUPPORT_CATEGORIES } from "@/components/support/SupportTicketDialog";

export default function SupportPage() {
  const { isSignedIn, isLoaded } = useUser();
  const { openSupport } = useSupport();

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
          <p>We&rsquo;re here to help. Send us a message and we&rsquo;ll get back to you.</p>
        </div>

        {isLoaded && isSignedIn ? (
          // Signed-in: real form that POSTs an enriched ticket to the backend.
          <div className="support-form">
            <p className="mb-4 text-muted-foreground">
              Submitting while signed in attaches your account + diagnostics automatically, so we can help faster.
            </p>
            <button type="button" className="support-submit" onClick={() => openSupport()}>
              Contact support
            </button>
          </div>
        ) : (
          // Signed-out: simple mailto form (no backend enrichment available).
          <form
            className="support-form"
            action={SUPPORT_MAILTO}
            method="POST"
            encType="text/plain"
            onSubmit={() => capture("support_contact_clicked", { source: "support_page_form" })}
          >
            <div className="support-field">
              <label htmlFor="name">Name</label>
              <input type="text" id="name" name="name" required autoComplete="name" placeholder="Your name" />
            </div>

            <div className="support-field">
              <label htmlFor="email">Email</label>
              <input type="email" id="email" name="email" required autoComplete="email" placeholder="you@example.com" />
            </div>

            <div className="support-field">
              <label htmlFor="subject">Subject</label>
              <select id="subject" name="subject" required defaultValue="">
                <option value="" disabled>Select a topic</option>
                {SUPPORT_CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>

            <div className="support-field">
              <label htmlFor="message">Message</label>
              <textarea id="message" name="message" required placeholder="Describe how we can help..." />
            </div>

            <button type="submit" className="support-submit">Send message</button>
          </form>
        )}

        <div className="support-direct">
          <p>You can also reach us directly at <a
            href={SUPPORT_MAILTO}
            onClick={() => capture("support_contact_clicked", { source: "support_page_direct" })}
          >{SUPPORT_EMAIL}</a></p>
        </div>

        {/* Declared as the data deletion instructions URL for the Meta app
            (App settings -> Basic). Reviewers open this section directly. */}
        <div className="support-direct" id="data-deletion">
          <h2>Deleting your data</h2>
          <p>
            To delete your Lightbulb account and everything we hold for it &mdash; your workspace,
            your agents, their history, and the credentials for any service you connected
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
