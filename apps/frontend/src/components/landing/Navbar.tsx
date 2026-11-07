"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useAuth, UserButton } from "@clerk/nextjs";

import { Logo } from "@/components/Logo";
import { hydrateFromStorage, strike, useLampTheme } from "@/components/landing/HeroLamp/lampStore";
import { captureSignupCta } from "@/lib/signupFlow";

export function Navbar() {
  const linksRef = useRef<HTMLDivElement>(null);
  const [mobileOpen, setMobileOpen] = useState(false);
  const { isSignedIn, isLoaded } = useAuth();
  const pathname = usePathname() ?? "/";
  const theme = useLampTheme();
  // Only `.ld-page` routes read data-lb-theme; on /marketplace (no .ld-page)
  // the toggle would flip the attribute and change nothing. Derived from the
  // route, not discovered from the DOM — the surface is a property of the page.
  const hasThemeSurface = !pathname.startsWith("/marketplace");
  useEffect(() => {
    hydrateFromStorage(); // adopt a stored choice into the store; the inline script already painted it
  }, []);
  const onLanding = pathname === "/";
  const onMarketplace = pathname.startsWith("/marketplace");
  const onBlog = pathname.startsWith("/blog");
  const onDocs = pathname.startsWith("/docs");

  // From a non-landing route, scroll anchors must navigate back to "/" first.
  const anchor = (id: string) => (onLanding ? `#${id}` : `/#${id}`);

  useEffect(() => {
    if (!onLanding) return; // Active-state observer only meaningful on the landing page.
    const linksEl = linksRef.current;
    if (!linksEl) return;

    const links = linksEl.querySelectorAll<HTMLAnchorElement>("a[data-section]");

    const clickHandlers: Array<() => void> = [];
    links.forEach((link) => {
      const handler = () => {
        links.forEach((l) => l.classList.remove("active"));
        link.classList.add("active");
        setMobileOpen(false);
      };
      link.addEventListener("click", handler);
      clickHandlers.push(handler);
    });

    const sections = [
      { id: "home", el: document.querySelector(".ld-funnel") },
      { id: "workspace", el: document.getElementById("workspace") },
      { id: "marketplace", el: document.getElementById("marketplace") },
      { id: "pricing", el: document.getElementById("pricing") },
    ].filter((s) => s.el);

    // The map's key is the nav's own vocabulary — the hero's DOM id is
    // "start" (the funnel anchor), which no link carries.
    const sectionKeyByEl = new Map(sections.map((s) => [s.el, s.id]));

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const id = sectionKeyByEl.get(entry.target) ?? "home";
            links.forEach((l) => {
              l.classList.toggle("active", l.dataset.section === id);
            });
          }
        });
      },
      { threshold: 0.35 }
    );

    sections.forEach((s) => observer.observe(s.el!));

    return () => {
      observer.disconnect();
      links.forEach((link, i) => {
        link.removeEventListener("click", clickHandlers[i]);
      });
    };
  }, [onLanding]);

  // Close mobile menu on resize to desktop
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 769px)");
    const handler = () => { if (mq.matches) setMobileOpen(false); };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  const initialActive = (id: string) => {
    // The landing hero is no longer a nav destination; the observer takes over on scroll.
    if (onLanding) return undefined;
    if (onMarketplace) return id === "marketplace" ? "active" : undefined;
    if (onBlog) return id === "blog" ? "active" : undefined;
    if (onDocs) return id === "docs" ? "active" : undefined;
    if (pathname.startsWith("/pricing")) return id === "pricing" ? "active" : undefined;
    return undefined;
  };

  return (
    <>
    <nav className="landing-nav" aria-label="Main navigation">
      <div className="nav-brand">
        <Logo size="md" withMark />
      </div>
      <div className={`nav-links${mobileOpen ? " mobile-open" : ""}`} ref={linksRef}>
        <Link href={anchor("workspace")} data-section="workspace" className={initialActive("workspace")}>
          Product
        </Link>
        <Link href="/docs" data-section="docs" className={initialActive("docs")}>
          Docs
        </Link>
        <Link href="/blog" data-section="blog" className={initialActive("blog")}>
          Blog
        </Link>
        <Link href="/network" data-section="network" className={initialActive("network")}>
          Network
        </Link>
        <Link href="/pricing" data-section="pricing" className={initialActive("pricing")}>
          Pricing
        </Link>
        {isLoaded && !isSignedIn && (
          <>
            <Link href="/sign-in" className="nav-mobile-login">
              Log in
            </Link>
            <Link
              href="/sign-up"
              className="nav-mobile-login"
              onClick={() =>
                captureSignupCta({ ctaId: "nav_mobile_sign_up", label: "Try free", surface: "landing_nav_mobile" })
              }
            >
              Try free
            </Link>
          </>
        )}
        {isLoaded && isSignedIn && (
          <Link href="/chat" className="nav-mobile-login">
            Dashboard
          </Link>
        )}
      </div>
      <div className="nav-right">
        {hasThemeSurface && (
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
        )}
        {isLoaded && isSignedIn ? (
          <>
            <Link href="/chat" className="btn-primary">
              Dashboard
            </Link>
            <UserButton />
          </>
        ) : (
          <>
            <Link href="/sign-in" className="nav-login">
              Log in
            </Link>
            <Link
              href="/request-setup"
              className="nav-login"
              onClick={() =>
                captureSignupCta({
                  ctaId: "nav_request_setup",
                  label: "Request setup",
                  surface: "landing_nav",
                  destination: "/request-setup",
                })
              }
            >
              Request setup
            </Link>
            {/* Self-serve is the one filled button, matching the hero. */}
            <Link
              href="/sign-up"
              className="btn-primary"
              onClick={() =>
                captureSignupCta({
                  ctaId: "nav_sign_up",
                  label: "Try free",
                  surface: "landing_nav",
                })
              }
            >
              Try free
            </Link>
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
