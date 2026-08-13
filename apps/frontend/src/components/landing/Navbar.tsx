"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useAuth, UserButton } from "@clerk/nextjs";

import { Logo } from "@/components/Logo";
import { capture } from "@/lib/analytics";

export function Navbar() {
  const linksRef = useRef<HTMLDivElement>(null);
  const [mobileOpen, setMobileOpen] = useState(false);
  const { isSignedIn, isLoaded } = useAuth();
  const pathname = usePathname() ?? "/";
  const onLanding = pathname === "/";
  const onMarketplace = pathname.startsWith("/marketplace");
  const onBlog = pathname.startsWith("/blog");

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
    if (onLanding) return id === "home" ? "active" : undefined;
    if (onMarketplace) return id === "marketplace" ? "active" : undefined;
    if (onBlog) return id === "blog" ? "active" : undefined;
    return undefined;
  };

  return (
    <nav className="landing-nav" aria-label="Main navigation">
      <Logo size="md" />
      <div className={`nav-links${mobileOpen ? " mobile-open" : ""}`} ref={linksRef}>
        <Link href="/" data-section="home" className={initialActive("home")}>
          Home
        </Link>
        <Link href={anchor("workspace")} data-section="workspace" className={initialActive("workspace")}>
          Product
        </Link>
        <Link href={anchor("marketplace")} data-section="marketplace" className={initialActive("marketplace")}>
          Marketplace
        </Link>
        <Link href={anchor("pricing")} data-section="pricing" className={initialActive("pricing")}>
          Pricing
        </Link>
        <Link href="/blog" data-section="blog" className={initialActive("blog")}>
          Blog
        </Link>
        {isLoaded && !isSignedIn && (
          <Link href="/sign-in" className="nav-mobile-login">
            Log in
          </Link>
        )}
        {isLoaded && isSignedIn && (
          <Link href="/chat" className="nav-mobile-login">
            Dashboard
          </Link>
        )}
      </div>
      <div className="nav-right">
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
              href="/sign-up"
              className="btn-primary"
              onClick={() =>
                capture("signup_started", {
                  source: "landing_nav",
                  destination: "/sign-up",
                })
              }
            >
              Sign up
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
      {mobileOpen && <div className="nav-mobile-backdrop" onClick={() => setMobileOpen(false)} />}
    </nav>
  );
}
