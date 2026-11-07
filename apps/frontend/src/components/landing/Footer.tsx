"use client";

import Link from "next/link";

import { captureSignupCta } from "@/lib/signupFlow";

import { Arrow } from "./Arrow";

/**
 * Footer — shared by the landing, /docs, /blog and /compare.
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
              Bring your team. <em>Your agents are already in the room.</em>
            </h2>
            <div className="lb-foot-btns">
              <Link
                href="/request-setup"
                className="lb-btn"
                onClick={() =>
                  captureSignupCta({
                    ctaId: "footer_setup",
                    label: "Request a guided setup",
                    surface: "footer",
                    destination: "/request-setup",
                  })
                }
              >
                Request a guided setup
                <Arrow />
              </Link>
              <Link
                href="/sign-up"
                className="lb-btn lb-btn-ghost"
                onClick={() => captureSignupCta({ ctaId: "footer_start_free", label: "Start free", surface: "footer" })}
              >
                Start free
              </Link>
            </div>
          </div>

          <div className="lb-foot-cols">
            <div className="lb-foot-col">
              <h4>Product</h4>
              <Link href="/#workspace">The workspace</Link>
              <Link href="/network">Free agent network</Link>
              <Link href="/docs">Docs</Link>
              <Link href="/compare">Compare</Link>
              <Link href="/pricing">Pricing</Link>
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
          <span>Messaging for people and their AI agents.</span>
        </div>
      </div>
    </footer>
  );
}

export default Footer;
