import Link from "next/link";

export function Footer() {
  return (
    <footer className="ld-footer">
      <div className="ld-footer-inner">
        <div className="ld-footer-top">
          <div className="ld-footer-cta">
            <h2>
              Your pod is ready when <em>you are.</em>
            </h2>
            <Link href="/chat" className="ld-btn">
              Start your pod
              <span className="ld-btn-arrow" aria-hidden="true">
                →
              </span>
            </Link>
          </div>

          <div className="ld-footer-cols">
            <div className="ld-footer-col">
              <span>Product</span>
              <Link href="/#features">Features</Link>
              <Link href="/#team">Team</Link>
              <Link href="/marketplace">Marketplace</Link>
              <Link href="/#pricing">Pricing</Link>
            </div>
            <div className="ld-footer-col">
              <span>Company</span>
              <Link href="/privacy">Privacy Policy</Link>
              <Link href="/terms">Terms of Service</Link>
              <Link href="/support">Support</Link>
            </div>
            <div className="ld-footer-col">
              <span>Legal</span>
              <Link href="/cookies">Cookies</Link>
              <Link href="/disclaimer">Disclaimer</Link>
              <Link href="/eula">EULA</Link>
              <Link href="/acceptable-use">Acceptable Use</Link>
              <Link href="/accessibility">Accessibility</Link>
            </div>
          </div>
        </div>

        <div className="ld-footer-base">
          <span>© {new Date().getFullYear()} isol8 Inc. All rights reserved.</span>
          <span>Private AI pods · built on OpenClaw</span>
        </div>

        <span className="ld-footer-word" aria-hidden="true">
          isol8
        </span>
      </div>
    </footer>
  );
}
