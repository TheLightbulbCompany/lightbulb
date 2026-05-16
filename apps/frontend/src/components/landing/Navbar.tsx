"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useAuth, UserButton } from "@clerk/nextjs";

import { Logo } from "@/components/Logo";

export function Navbar() {
  const linksRef = useRef<HTMLDivElement>(null);
  const [mobileOpen, setMobileOpen] = useState(false);
  const { isSignedIn, isLoaded } = useAuth();

  useEffect(() => {
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
      { id: "home", el: document.querySelector(".landing-hero") },
      { id: "features", el: document.getElementById("features") },
      { id: "team", el: document.getElementById("team") },
      { id: "pricing", el: document.getElementById("pricing") },
      { id: "faq", el: document.getElementById("faq") },
      { id: "goosetown", el: document.getElementById("goosetown") },
    ].filter((s) => s.el);

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const id = entry.target.id || "home";
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
  }, []);

  // Close mobile menu on resize to desktop
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 769px)");
    const handler = () => { if (mq.matches) setMobileOpen(false); };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  return (
    <nav className="landing-nav" aria-label="Main navigation">
      <Logo size="md" />
      <div className={`nav-links${mobileOpen ? " mobile-open" : ""}`} ref={linksRef}>
        <Link href="/" data-section="home" className="active">
          Home
        </Link>
        <Link href="#features" data-section="features">
          Features
        </Link>
        <Link href="#team" data-section="team">
          Team
        </Link>
        <Link href="/marketplace">
          Marketplace
        </Link>
        <Link href="#pricing" data-section="pricing">
          Pricing
        </Link>
        <Link href="#faq" data-section="faq">
          FAQ
        </Link>
        <Link href="#goosetown" data-section="goosetown">
          GooseTown<span className="nav-alpha">alpha</span>
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
            <Link href="/sign-up" className="btn-primary">
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
