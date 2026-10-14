import Link from "next/link";

import { Arrow } from "./Arrow";

/**
 * Footer — shared by the landing, /docs, /blog and /[seoSlug].
 *
 * The backdrop is a CSS gradient rather than a photograph: the illustrations we
 * had were ~900px wide and visibly stretched when bled full width. A gradient
 * has no native resolution, so it stays sharp at any viewport.
 */
export function Footer() {
  return (
    <footer className="lb-foot">
      <div className="lb-foot-in">
        <div className="lb-foot-top">
          <div className="lb-foot-cta">
            <h2>
              Your business is ready when <em>you are.</em>
            </h2>
            <Link href="/sign-up" className="lb-btn">
              Start your business
              <Arrow />
            </Link>
          </div>

          <div className="lb-foot-cols">
            <div className="lb-foot-col">
              <h4>Product</h4>
              <Link href="/#workspace">The workspace</Link>
              <Link href="/marketplace">Marketplace</Link>
              <Link href="/docs">Docs</Link>
              <Link href="/#pricing">Pricing</Link>
              {/* API route 302s to the DMG — must be a hard navigation, not a client <Link>. */}
              {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
              <a href="/api/download/desktop" title="macOS · Apple Silicon">
                Download for Mac
              </a>
            </div>
            <div className="lb-foot-col">
              <h4>Company</h4>
              <Link href="/blog">Blog</Link>
              <Link href="/security">Your data</Link>
              <Link href="/privacy">Privacy Policy</Link>
              <Link href="/terms">Terms of Service</Link>
              <Link href="/support">Support</Link>
            </div>
            <div className="lb-foot-col">
              <h4>Legal</h4>
              <Link href="/cookies">Cookies</Link>
              <Link href="/disclaimer">Disclaimer</Link>
              <Link href="/eula">EULA</Link>
              <Link href="/acceptable-use">Acceptable Use</Link>
              <Link href="/accessibility">Accessibility</Link>
            </div>
          </div>
        </div>

        <div className="lb-foot-base">
          <span>
            © {new Date().getFullYear()} Isol8 Inc., doing business as Lightbulb AI. All rights reserved.
          </span>
          <span>Autonomous businesses</span>
        </div>
      </div>
    </footer>
  );
}

export default Footer;
