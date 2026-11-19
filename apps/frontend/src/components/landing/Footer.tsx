"use client";

import Link from "next/link";

import { captureSignupCta } from "@/lib/signupFlow";

import { Arrow } from "./Arrow";

/**
 * Footer, shared by the marketing pages. It carries the page's final call to
 * action, so the homepage has no separate closing section.
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
              Get back to your client. <em>Your assistant has the phone.</em>
            </h2>
            <div className="lb-foot-btns">
              <Link
                href="/sign-up"
                className="lb-btn"
                onClick={() =>
                  captureSignupCta({ ctaId: "footer_start_free", label: "Set up your assistant", surface: "footer" })
                }
              >
                Set up your assistant
                <Arrow />
              </Link>
              <Link
                href="/request-setup"
                className="lb-btn lb-btn-ghost"
                onClick={() =>
                  captureSignupCta({
                    ctaId: "footer_setup",
                    label: "Talk to a founder",
                    surface: "footer",
                    destination: "/request-setup",
                  })
                }
              >
                Talk to a founder
              </Link>
            </div>
          </div>

          <div className="lb-foot-cols">
            <div className="lb-foot-col">
              <h4>Product</h4>
              <Link href="/salons">For salons</Link>
              <Link href="/spas">For spas</Link>
              <Link href="/barbershops">For barbershops</Link>
              <Link href="/pricing">Pricing</Link>
              <Link href="/compare">Compare</Link>
              <Link href="/request-setup">Talk to a founder</Link>
            </div>
            <div className="lb-foot-col">
              <h4>Help</h4>
              <Link href="/support">Support</Link>
              <Link href="/security">Security and your data</Link>
              <Link href="/sign-in">Log in</Link>
              <Link href="/sign-up">Sign up</Link>
              <Link href="/network">Developer network</Link>
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
          <span>
            © {new Date().getFullYear()} Isol8 Inc., doing business as Lightbulb AI. All rights reserved.
          </span>
          <span>Calls answered and appointments booked for salons, spas and barbershops.</span>
        </div>
      </div>
    </footer>
  );
}

export default Footer;
