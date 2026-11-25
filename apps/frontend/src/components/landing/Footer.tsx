import Link from "next/link";

/**
 * Footer, shared by the marketing pages.
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
          <div className="lb-foot-cols">
            <div className="lb-foot-col">
              <h4>Product</h4>
              <Link href="/os">Spark</Link>
            </div>
            <div className="lb-foot-col">
              <h4>Help</h4>
              <Link href="/support">Support</Link>
            </div>
            <div className="lb-foot-col">
              <h4>Legal</h4>
              <Link href="/privacy">Privacy Policy</Link>
              <Link href="/terms">Terms of Service</Link>
              <Link href="/cookies">Cookies</Link>
              <Link href="/disclaimer">Disclaimer</Link>
              <Link href="/eula">EULA</Link>
              <Link href="/acceptable-use">Acceptable Use</Link>
              <Link href="/accessibility">Accessibility</Link>
            </div>
          </div>
        </div>

        <div className="lb-foot-base">
          <span>© {new Date().getFullYear()} Isol8 Inc., doing business as Lightbulb AI. All rights reserved.</span>
        </div>
      </div>
    </footer>
  );
}

export default Footer;
