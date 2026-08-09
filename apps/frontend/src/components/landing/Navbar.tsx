"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useAuth, UserButton } from "@clerk/nextjs";

import { Logo } from "@/components/Logo";
import { capture } from "@/lib/analytics";

const IN_PAGE_NAV_ITEMS = [
  { id: "home", label: "Home" },
  { id: "team", label: "Team" },
  { id: "marketplace", label: "Marketplace" },
  { id: "features", label: "Features" },
  { id: "pricing", label: "Pricing" },
  { id: "faq", label: "FAQ" },
  { id: "goosetown", label: "GooseTown", suffix: <span className="nav-alpha">alpha</span> },
] as const;

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

    const setActiveSection = (id: string) => {
      links.forEach((l) => {
        l.classList.toggle("active", l.dataset.section === id);
      });
    };

    const clickHandlers: Array<(event: MouseEvent) => void> = [];
    links.forEach((link) => {
      const handler = (event: MouseEvent) => {
        const sectionId = link.dataset.section ?? "home";
        if (onLanding && sectionId !== "blog" && !event.metaKey && !event.ctrlKey && !event.shiftKey && event.button === 0) {
          event.preventDefault();
          if (sectionId === "home") {
            window.history.pushState(null, "", "/");
            window.scrollTo({ top: 0, behavior: "smooth" });
          } else {
            document.getElementById(sectionId)?.scrollIntoView({ behavior: "smooth", block: "start" });
            window.history.pushState(null, "", `#${sectionId}`);
          }
        }
        setActiveSection(sectionId);
        setMobileOpen(false);
      };
      link.addEventListener("click", handler);
      clickHandlers.push(handler);
    });

    const sections = IN_PAGE_NAV_ITEMS.map(({ id }) => ({
      id,
      el: id === "home" ? document.querySelector(".ld-hero") : document.getElementById(id),
    })).filter((s) => s.el);

    const updateActiveFromScroll = () => {
      const navHeight = document.querySelector<HTMLElement>(".landing-nav")?.offsetHeight ?? 0;
      const activationLine = window.scrollY + navHeight + 24;
      const active = sections.reduce((current, section) => {
        const top = section.el!.getBoundingClientRect().top + window.scrollY;
        return top <= activationLine ? section.id : current;
      }, "home");
      setActiveSection(active);
    };

    updateActiveFromScroll();
    window.addEventListener("scroll", updateActiveFromScroll, { passive: true });
    window.addEventListener("hashchange", updateActiveFromScroll);

    /* Keep an observer as a low-cost nudge after anchor jumps and layout shifts;
       the scroll-position pass above owns ordering so active state progresses in
       document order even when IntersectionObserver batches entries out of order. */
    const sectionsForObserver = [
      { id: "home", el: document.querySelector(".ld-hero") },
      { id: "team", el: document.getElementById("team") },
      { id: "marketplace", el: document.getElementById("marketplace") },
      { id: "features", el: document.getElementById("features") },
      { id: "pricing", el: document.getElementById("pricing") },
      { id: "faq", el: document.getElementById("faq") },
      { id: "goosetown", el: document.getElementById("goosetown") },
    ].filter((s) => s.el);

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            updateActiveFromScroll();
          }
        });
      },
      { threshold: 0.35 }
    );

    sectionsForObserver.forEach((s) => observer.observe(s.el!));

    return () => {
      observer.disconnect();
      window.removeEventListener("scroll", updateActiveFromScroll);
      window.removeEventListener("hashchange", updateActiveFromScroll);
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
        {IN_PAGE_NAV_ITEMS.map(({ id, label, suffix }) => (
          <Link key={id} href={id === "home" ? "/" : anchor(id)} data-section={id} className={initialActive(id)}>
            {label}{suffix}
          </Link>
        ))}
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
