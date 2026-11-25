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

        <div className="support-direct" id="data-deletion">
          <h2>Deleting your data</h2>
          <p>
            To delete your Spark account and what we hold for it (your account record and your
            saved conversation text), choose Delete account in Spark, or email <a
              href={`${SUPPORT_MAILTO}?subject=Delete%20my%20account`}
              onClick={() => capture("support_contact_clicked", { source: "support_page_data_deletion" })}
            >{SUPPORT_EMAIL}</a> from your account email address. Deleting an account cannot be undone.
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
