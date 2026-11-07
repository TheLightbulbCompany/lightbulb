import { SignUp } from "@clerk/nextjs";
import Link from "next/link";

import { Logo } from "@/components/Logo";
import { SignUpDoors } from "@/components/onboarding/SignUpDoors";
import { SignupPlanPill } from "@/components/SignupPlanPill";

export default function Page() {
  return (
    <>
      <style>{`
        /* ── LAYOUT ── */
        .auth-layout {
          display: grid;
          grid-template-columns: 1fr 1fr;
          min-height: 100vh;
        }

        /* ── LEFT PANEL ── */
        /* The brand panel is the dark half in both themes. On the dark ladder --midnight is
           the light text colour, so re-point the two names it uses instead of inverting. */
        :root[data-chat-theme="dark"] .auth-brand {
          --midnight: var(--plane-rail);
          --cream: var(--fg);
        }

        .auth-brand {
          background: var(--midnight);
          display: flex;
          flex-direction: column;
          justify-content: space-between;
          padding: 48px;
          position: relative;
          overflow: hidden;
        }
        .auth-brand-top { position: relative; z-index: 1; }

        .auth-brand-middle {
          position: relative;
          z-index: 1;
          flex: 1;
          display: flex;
          flex-direction: column;
          justify-content: center;
        }
        .auth-eyebrow {
          font-family: var(--font-sans), system-ui, sans-serif;
          font-size: 11px;
          font-weight: 700;
          letter-spacing: 4px;
          text-transform: uppercase;
          color: var(--brass);
          margin-bottom: 20px;
        }
        .auth-tagline {
          font-family: var(--font-serif), 'Lora', serif;
          font-size: clamp(32px, 3.5vw, 48px);
          font-weight: 400;
          line-height: 1.15;
          letter-spacing: -.02em;
          color: var(--cream);
          margin-bottom: 28px;
        }
        .auth-tagline em {
          font-style: italic;
          color: var(--cream);
        }

        /* ── FEATURE PILLS ── */
        .auth-features {
          display: flex;
          flex-direction: column;
          gap: 16px;
          max-width: 340px;
        }
        .auth-feat {
          display: flex;
          align-items: flex-start;
          gap: 14px;
          opacity: 0;
          transform: translateX(-16px);
          animation: auth-feat-in 500ms ease-out forwards;
        }
        .auth-feat:nth-child(1) { animation-delay: 300ms; }
        .auth-feat:nth-child(2) { animation-delay: 500ms; }
        .auth-feat:nth-child(3) { animation-delay: 700ms; }
        .auth-feat:nth-child(4) { animation-delay: 900ms; }
        @keyframes auth-feat-in {
          to { opacity: 1; transform: translateX(0); }
        }
        .auth-feat-icon {
          width: 36px;
          height: 36px;
          border-radius: 10px;
          background: color-mix(in srgb, var(--cream) 10%, transparent);
          border: 1px solid color-mix(in srgb, var(--cream) 14%, transparent);
          display: flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
        }
        .auth-feat-title {
          font-family: var(--font-sans), system-ui, sans-serif;
          font-size: 14px;
          font-weight: 600;
          color: var(--cream);
          margin-bottom: 2px;
          line-height: 1.4;
        }
        .auth-feat-desc {
          font-family: var(--font-sans), system-ui, sans-serif;
          font-size: 13px;
          color: color-mix(in srgb, var(--cream) 70%, var(--midnight));
          line-height: 1.5;
        }

        /* ── RIGHT PANEL ── */
        .auth-form-panel {
          background: var(--cream);
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          padding: 48px;
          position: relative;
          overflow-y: auto;
        }
        .auth-form-panel::before {
          content: '';
          position: absolute;
          top: 60px;
          right: 60px;
          width: 200px;
          height: 200px;
          background: color-mix(in srgb, var(--midnight) 3%, transparent);
          border-radius: 50%;
          filter: blur(60px);
          pointer-events: none;
        }

        .auth-card {
          width: 100%;
          max-width: 400px;
          position: relative;
          z-index: 1;
          display: flex;
          flex-direction: column;
          align-items: center;
        }

        .auth-mobile-logo {
          display: none;
          margin-bottom: 32px;
        }

        .auth-footer-links {
          margin-top: 24px;
          text-align: center;
          font-family: var(--font-sans), system-ui, sans-serif;
          font-size: 14px;
          color: var(--midnight-muted);
          line-height: 1.5;
        }
        .auth-footer-links a {
          color: var(--midnight);
          font-weight: 600;
          text-decoration: none;
          transition: color 200ms ease-out;
        }
        .auth-footer-links a:hover { color: var(--midnight-soft); }

        .auth-doors {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 8px;
          width: 100%;
          margin-bottom: 20px;
        }
        .auth-door {
          display: flex;
          flex-direction: column;
          gap: 4px;
          padding: 12px;
          text-align: left;
          border: 1px solid color-mix(in srgb, var(--midnight) 14%, transparent);
          border-radius: 10px;
          background: transparent;
          font-family: var(--font-sans), system-ui, sans-serif;
          cursor: pointer;
        }
        .auth-door[aria-selected="true"] { border-color: var(--midnight); }
        .auth-door-title { font-size: 14px; font-weight: 600; color: var(--midnight); }
        .auth-door-body { font-size: 12px; line-height: 1.4; color: var(--midnight-muted); }

        .auth-join {
          margin-top: 24px;
          width: 100%;
          display: flex;
          flex-direction: column;
          gap: 8px;
          font-family: var(--font-sans), system-ui, sans-serif;
          font-size: 13px;
          color: var(--midnight-muted);
          line-height: 1.5;
        }

        .auth-legal-links {
          margin-top: 16px;
          text-align: center;
          font-family: var(--font-sans), system-ui, sans-serif;
          font-size: 12px;
          color: var(--midnight-muted);
          line-height: 1.5;
        }
        .auth-legal-links a {
          color: var(--midnight-muted);
          text-decoration: none;
          transition: color 200ms ease-out;
        }
        .auth-legal-links a:hover { color: var(--midnight); }

        /* ── MOBILE ── */
        @media (max-width: 767px) {
          .auth-layout { grid-template-columns: 1fr; }
          .auth-brand { display: none; }
          .auth-form-panel {
            min-height: 100vh;
            padding: 32px 24px;
            align-items: center;
            justify-content: flex-start;
            padding-top: 64px;
          }
          .auth-card { max-width: 380px; }
          .auth-mobile-logo { display: flex; }
        }

        /* ── REDUCED MOTION ── */
        @media (prefers-reduced-motion: reduce) {
          .auth-feat {
            animation-duration: 0.01ms !important;
            animation-delay: 0ms !important;
          }
        }
      `}</style>

      <div className="auth-layout">
        {/* LEFT: BRAND PANEL */}
        <div className="auth-brand" aria-hidden="true" inert>
          <div className="auth-brand-top">
            <Logo tone="cream" size="lg" withMark />
          </div>

          <div className="auth-brand-middle">
            <div className="auth-eyebrow">Get started</div>
            <h1 className="auth-tagline">Bring your team.</h1>

            <div className="auth-features" role="list">
              <div className="auth-feat" role="listitem">
                <div className="auth-feat-icon" aria-hidden="true">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--cream)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="12" cy="12" r="10" />
                    <polyline points="12 6 12 12 16 14" />
                  </svg>
                </div>
                {/* Was "Always running / Works in the background, even when
                    you're offline." That is a PAID-plan property: a free-tier
                    container parks when idle, so it promised the one thing the
                    tier the reader is about to get does not do. Replaced with
                    the cost line, which this flow never stated anywhere — but
                    branched, because a TEAM signup gets no free tier at all
                    (see SignupPlanPill). */}
                <SignupPlanPill />
              </div>
              <div className="auth-feat" role="listitem">
                <div className="auth-feat-icon" aria-hidden="true">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--cream)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                    <circle cx="9" cy="7" r="4" />
                    <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
                    <path d="M16 3.13a4 4 0 0 1 0 7.75" />
                  </svg>
                </div>
                <div>
                  <div className="auth-feat-title">Your team in the same rooms</div>
                  <div className="auth-feat-desc">The first five people are included in the plan.</div>
                </div>
              </div>
              <div className="auth-feat" role="listitem">
                <div className="auth-feat-icon" aria-hidden="true">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--cream)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
                    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  </svg>
                </div>
                <div>
                  <div className="auth-feat-title">Your own cloud</div>
                  <div className="auth-feat-desc">Each workspace runs in its own isolated environment.</div>
                </div>
              </div>
              <div className="auth-feat" role="listitem">
                <div className="auth-feat-icon" aria-hidden="true">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--cream)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 2L2 7l10 5 10-5-10-5z" />
                    <path d="M2 17l10 5 10-5" />
                    <path d="M2 12l10 5 10-5" />
                  </svg>
                </div>
                <div>
                  <div className="auth-feat-title">Agents that ask first</div>
                  <div className="auth-feat-desc">Plans, spending and new hires wait for a yes.</div>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* RIGHT: FORM PANEL */}
        <div className="auth-form-panel">
          <div className="auth-card">
            {/* Mobile-only logo */}
            <div className="auth-mobile-logo">
              <Logo size="md" withMark />
            </div>

            <SignUpDoors createForm={<SignUp fallbackRedirectUrl="/voice" signInFallbackRedirectUrl="/voice" />} />

            <div className="auth-footer-links">
              Already have an account?{" "}
              <Link href="/sign-in">Sign in</Link>
            </div>

            <div className="auth-legal-links">
              <Link href="/terms">Terms of Service</Link>
              {" · "}
              <Link href="/privacy">Privacy Policy</Link>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
