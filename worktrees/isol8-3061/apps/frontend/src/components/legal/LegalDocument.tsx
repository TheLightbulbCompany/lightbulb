import Link from "next/link";

import { Logo } from "@/components/Logo";
import { TermlyEmbed } from "@/components/legal/TermlyEmbed";

type LegalDocumentProps = {
  dataId: string;
};

/**
 * Shared chrome for Termly-embedded legal pages. Mirrors the layout of the
 * hand-written legal pages (topbar + divider + content + footer). Termly
 * renders the document's own title, so we intentionally omit an <h1> here.
 */
export function LegalDocument({ dataId }: LegalDocumentProps) {
  return (
    <div className="legal-page">
      <header className="legal-topbar">
        <Logo size="md" />
        <Link href="/" className="legal-btn-back">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <path d="M10 12L6 8L10 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Back
        </Link>
      </header>

      <hr className="legal-divider" />

      <main className="legal-content">
        <TermlyEmbed dataId={dataId} />
      </main>

      <footer className="legal-footer">
        <p>&copy; {new Date().getFullYear()} Isol8 Inc., doing business as Lightbulb AI. All rights reserved.</p>
        <nav className="legal-footer-links">
          <Link href="/privacy">Privacy Policy</Link>
          <Link href="/terms">Terms of Service</Link>
          <Link href="/support">Support</Link>
        </nav>
      </footer>
    </div>
  );
}
