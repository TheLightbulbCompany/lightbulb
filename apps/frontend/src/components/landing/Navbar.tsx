"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { Logo } from "@/components/Logo";
import { hydrateFromStorage, strike, useLampTheme } from "@/components/landing/HeroLamp/lampStore";
import { trackSparkDownload, useOnPhone } from "@/app/os/SparkDownload";

// Pages that render Spark's page, and so carry its "open this on your Mac" line (#get).
const onSparkPage = (pathname: string) => pathname === "/" || pathname === "/os";

export function Navbar() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const pathname = usePathname() ?? "/";
  const theme = useLampTheme();
  const onPhone = useOnPhone();
  // A phone cannot install Spark: the button goes to the page's "open this on your Mac" line instead.
  const ctaHref = onPhone ? (onSparkPage(pathname) ? "#get" : "/os#get") : "/download/spark";
  const onCta = () => {
    // The button can land on the page already open, so the phone menu must close itself.
    setMobileOpen(false);
    if (!onPhone) trackSparkDownload("nav");
  };
  // The Spark download is a redirect to a file, so a plain link (no prefetch).
  const ctaLink = (className: string, children: React.ReactNode) => (
    <a href={ctaHref} className={className} onClick={onCta}>
      {children}
    </a>
  );
  useEffect(() => {
    hydrateFromStorage(); // adopt a stored choice into the store; the inline script already painted it
  }, []);

  // The phone menu closes on a new page (reset during render, not in an effect).
  const [menuPath, setMenuPath] = useState(pathname);
  if (menuPath !== pathname) {
    setMenuPath(pathname);
    setMobileOpen(false);
  }

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
        <Link
          href="/os"
          className={onSparkPage(pathname) ? "active" : undefined}
          onClick={() => setMobileOpen(false)}
        >
          Spark
        </Link>
        {ctaLink("nav-mobile-login", "Download for Mac")}
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
        {/* The long label would push the bar past a phone's width; phones get the short one. */}
        {ctaLink(
          "btn-primary",
          <>
            <span className="nav-cta-long">Download for Mac</span>
            <span className="nav-cta-short">Download</span>
          </>,
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
