"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { useAuth, UserButton } from "@clerk/nextjs";

import { Logo } from "@/components/Logo";
import { hydrateFromStorage, strike, useLampTheme } from "@/components/landing/HeroLamp/lampStore";
import { captureSignupCta } from "@/lib/signupFlow";
import { trackSparkDownload, useOnPhone } from "@/app/os/SparkDownload";

const LINKS = [
  { href: "/phone", label: "Phone" },
  { href: "/meetings", label: "Meetings" },
  { href: "/os", label: "Spark" },
  { href: "/pricing", label: "Pricing" },
] as const;

type Cta = { product: "phone" | "spark" | "meetings"; label: string; short: string; href: string };

// The main button follows the page: Spark's download on Spark pages, the waitlist on Meetings, and
// Lightbulb Phone's sign-up everywhere else.
export function navCta(pathname: string): Cta {
  if (pathname === "/os" || pathname.startsWith("/os/")) {
    return { product: "spark", label: "Download for Mac", short: "Download", href: "/download/spark" };
  }
  if (pathname === "/meetings") {
    return { product: "meetings", label: "Join the waitlist", short: "Waitlist", href: "/meetings#waitlist" };
  }
  return { product: "phone", label: "Set up Lightbulb Phone", short: "Set up", href: "/sign-up" };
}

export function Navbar() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const { isSignedIn, isLoaded } = useAuth();
  const pathname = usePathname() ?? "/";
  const theme = useLampTheme();
  const cta = navCta(pathname);
  const spark = cta.product === "spark";
  const onPhone = useOnPhone();
  // A phone cannot install Spark: the button goes to the page's "open this on your Mac" line instead.
  const ctaHref = spark && onPhone ? (pathname === "/os" ? "#get" : "/os#get") : cta.href;
  const onCta = (ctaId: string, surface: string) => () => {
    // The Spark and waitlist buttons can land on the page already open, so the phone menu must close itself.
    setMobileOpen(false);
    if (spark) {
      if (!onPhone) trackSparkDownload("nav");
    } else if (cta.product === "phone") {
      captureSignupCta({ ctaId, label: cta.label, surface });
    }
  };
  // Spark's account lives in the Mac app, so Spark pages offer no web sign-in and no way into the Phone app.
  const signedIn = isLoaded && isSignedIn && !spark;
  const signedOut = spark || (isLoaded && !isSignedIn);
  // The Spark download is a redirect to a file, so a plain link (no prefetch); the rest are pages.
  const ctaLink = (className: string, ctaId: string, surface: string, children: React.ReactNode) =>
    spark ? (
      <a href={ctaHref} className={className} onClick={onCta(ctaId, surface)}>
        {children}
      </a>
    ) : (
      <Link href={ctaHref} className={className} onClick={onCta(ctaId, surface)}>
        {children}
      </Link>
    );
  useEffect(() => {
    hydrateFromStorage(); // adopt a stored choice into the store; the inline script already painted it
  }, []);

  // Close mobile menu on resize to desktop
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 769px)");
    const handler = () => { if (mq.matches) setMobileOpen(false); };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  return (
    <>
    <nav className="landing-nav" aria-label="Main navigation">
      <div className="nav-brand">
        <Logo size="md" withMark />
      </div>
      <div className={`nav-links${mobileOpen ? " mobile-open" : ""}`}>
        {LINKS.map((l) => (
          <Link
            key={l.href}
            href={l.href}
            className={pathname.startsWith(l.href) ? "active" : undefined}
            onClick={() => setMobileOpen(false)}
          >
            {l.label}
          </Link>
        ))}
        {signedOut && (
          <>
            {!spark && (
              <Link href="/sign-in" className="nav-mobile-login">
                Log in
              </Link>
            )}
            {ctaLink("nav-mobile-login", "nav_mobile_sign_up", "landing_nav_mobile", cta.label)}
          </>
        )}
        {signedIn && (
          <Link href="/voice" className="nav-mobile-login">
            Open Lightbulb
          </Link>
        )}
      </div>
      <div className="nav-right">
        <button
          type="button"
          className="nav-theme"
          onClick={strike}
          aria-pressed={theme === "dark"}
          aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          title={theme === "dark" ? "Light mode" : "Dark mode"}
        >
          {theme === "dark" ? (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <circle cx="12" cy="12" r="4" stroke="currentColor" strokeWidth="1.6" />
              <path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M5.3 18.7l1.8-1.8M16.9 7.1l1.8-1.8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          ) : (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M20.5 14.2A8.5 8.5 0 0 1 9.8 3.5a8.5 8.5 0 1 0 10.7 10.7Z" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
            </svg>
          )}
        </button>
        {signedIn ? (
          <>
            <Link href="/voice" className="btn-primary">
              Open Lightbulb
            </Link>
            <UserButton />
          </>
        ) : (
          <>
            {!spark && (
              <Link href="/sign-in" className="nav-login">
                Log in
              </Link>
            )}
            <Link
              href="/request-setup"
              className="nav-login"
              onClick={() =>
                captureSignupCta({
                  ctaId: "nav_request_setup",
                  label: "Talk to a founder",
                  surface: "landing_nav",
                  destination: "/request-setup",
                })
              }
            >
              Talk to a founder
            </Link>
            {/* Self-serve is the one filled button, matching the hero. */}
            {/* The long label would push the bar past a phone's width; phones get the short one. */}
            {ctaLink(
              "btn-primary",
              "nav_sign_up",
              "landing_nav",
              <>
                <span className="nav-cta-long">{cta.label}</span>
                <span className="nav-cta-short">{cta.short}</span>
              </>,
            )}
          </>
        )}
        <button
          className="nav-hamburger"
          onClick={() => setMobileOpen((v) => !v)}
          aria-label={mobileOpen ? "Close menu" : "Open menu"}
          aria-expanded={mobileOpen}
        >
          <span className={`hamburger-line${mobileOpen ? " open" : ""}`} />
          <span className={`hamburger-line${mobileOpen ? " open" : ""}`} />
          <span className={`hamburger-line${mobileOpen ? " open" : ""}`} />
        </button>
      </div>
    </nav>
    {/* Outside the nav: its backdrop-filter makes it the containing block for
        fixed children, which shrank this backdrop to the bar's own box. */}
    {mobileOpen && <div className="nav-mobile-backdrop" onClick={() => setMobileOpen(false)} />}
    </>
  );
}
