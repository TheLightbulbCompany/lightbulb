"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useAuth, UserButton } from "@clerk/nextjs";

import { Logo } from "@/components/Logo";
import { hydrateFromStorage, strike, useLampTheme } from "@/components/landing/HeroLamp/lampStore";
import { captureSignupCta } from "@/lib/signupFlow";
import { trackSparkDownload, useOnPhone } from "@/app/os/SparkDownload";

// One Products menu, not a link per product (owner, 2026-11-24).
export const PRODUCTS = [
  { href: "/phone", name: "Lightbulb Phone", line: "Answers your business line and books appointments." },
  { href: "/meetings", name: "Lightbulb Meetings", line: "Joins your Google Meet and keeps the notes." },
  { href: "/os", name: "Spark", line: "One-click voice interface for your Mac." },
] as const;

const onPage = (pathname: string, href: string) => pathname === href || pathname.startsWith(`${href}/`);

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
  const [productsOpen, setProductsOpen] = useState(false);
  const productsRef = useRef<HTMLDivElement>(null);
  const productsBtn = useRef<HTMLButtonElement>(null);
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

  // Both menus close on a new page (reset during render, not in an effect); the Products panel also
  // closes on Escape (focus back to its button) and on a click outside it.
  const [menuPath, setMenuPath] = useState(pathname);
  if (menuPath !== pathname) {
    setMenuPath(pathname);
    setProductsOpen(false);
    setMobileOpen(false);
  }
  useEffect(() => {
    if (!productsOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setProductsOpen(false);
      productsBtn.current?.focus();
    };
    const onDown = (e: PointerEvent) => {
      if (!productsRef.current?.contains(e.target as Node)) setProductsOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
    };
  }, [productsOpen]);

  const closeMenus = () => {
    setProductsOpen(false);
    setMobileOpen(false);
  };

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
        <div
          className="nav-products"
          ref={productsRef}
          onBlur={(e) => {
            // Tabbing out of the menu closes it.
            if (e.relatedTarget && !e.currentTarget.contains(e.relatedTarget as Node)) setProductsOpen(false);
          }}
        >
          <button
            type="button"
            ref={productsBtn}
            className={`nav-products-btn${PRODUCTS.some((p) => onPage(pathname, p.href)) ? " active" : ""}`}
            aria-expanded={productsOpen}
            aria-controls="nav-products-panel"
            onClick={() => setProductsOpen((v) => !v)}
          >
            Products
            <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true">
              <path d="M2 3.5 5 6.5l3-3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          {/* The phone menu has no dropdown: it shows this list under a plain label. */}
          <span className="nav-products-label" aria-hidden="true">Products</span>
          <div id="nav-products-panel" className={`nav-products-panel${productsOpen ? " open" : ""}`}>
            {PRODUCTS.map((p) => {
              const current = onPage(pathname, p.href);
              return (
                <Link
                  key={p.href}
                  href={p.href}
                  className={current ? "active" : undefined}
                  aria-current={current ? "page" : undefined}
                  onClick={closeMenus}
                >
                  <span className="nav-product-name">{p.name}</span>
                  <span className="nav-product-line">{p.line}</span>
                </Link>
              );
            })}
          </div>
        </div>
        <Link
          href="/pricing"
          className={onPage(pathname, "/pricing") ? "active" : undefined}
          onClick={closeMenus}
        >
          Pricing
        </Link>
        {signedOut && (
          <>
            {!spark && (
              <Link href="/sign-in" className="nav-mobile-login" onClick={closeMenus}>
                Log in
              </Link>
            )}
            {ctaLink("nav-mobile-login", "nav_mobile_sign_up", "landing_nav_mobile", cta.label)}
          </>
        )}
        {signedIn && (
          <Link href="/voice" className="nav-mobile-login" onClick={closeMenus}>
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
