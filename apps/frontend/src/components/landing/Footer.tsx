import Link from "next/link";

import { Logo } from "@/components/Logo";

export function Footer() {
  return (
    <footer className="landing-footer">
      <div className="footer-inner">
        <Logo tone="cream" size="sm" href={null} />
        <div className="footer-links">
          <Link href="/privacy">Privacy Policy</Link>
          <Link href="/terms">Terms of Service</Link>
          <Link href="/support">Support</Link>
        </div>
        <div className="footer-legal-cluster">
          <Link href="/cookies">Cookies</Link>
          <Link href="/disclaimer">Disclaimer</Link>
          <Link href="/eula">EULA</Link>
          <Link href="/acceptable-use">Acceptable Use</Link>
          <Link href="/accessibility">Accessibility</Link>
        </div>
        <span className="footer-copy">
          © {new Date().getFullYear()} isol8 Inc. All rights reserved.
        </span>
      </div>
    </footer>
  );
}
