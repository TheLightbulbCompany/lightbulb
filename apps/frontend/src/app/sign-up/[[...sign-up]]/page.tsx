import { SignUp } from "@clerk/nextjs";
import Link from "next/link";

import { Logo } from "@/components/Logo";

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
        .auth-brand {
          background: #06402B;
          display: flex;
          flex-direction: column;
          justify-content: space-between;
          padding: 48px;
          position: relative;
          overflow: hidden;
        }
        .auth-brand::before {
          content: '';
          position: absolute;
          bottom: -120px;
          right: -120px;
          width: 480px;
          height: 480px;
          background: rgba(74, 158, 116, .15);
          border-radius: 50%;
          filter: blur(100px);
          pointer-events: none;
        }
        .auth-brand::after {
          content: '';
          position: absolute;
          top: -80px;
          left: -80px;
          width: 320px;
          height: 320px;
          background: rgba(0, 0, 0, .15);
          border-radius: 50%;
          filter: blur(80px);
          pointer-events: none;
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
          font-family: var(--font-dm-sans), sans-serif;
          font-size: 11px;
          font-weight: 700;
          letter-spacing: 4px;
          text-transform: uppercase;
          color: #4a9e74;
          margin-bottom: 20px;
        }
        .auth-tagline {
          font-family: var(--font-lora-serif), 'Lora', serif;
          font-size: clamp(32px, 3.5vw, 48px);
          font-weight: 400;
          line-height: 1.15;
          letter-spacing: -.02em;
          color: white;
          margin-bottom: 28px;
        }
        .auth-tagline em {
          font-style: italic;
          color: #a8e6c6;
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
        @keyframes auth-feat-in {
          to { opacity: 1; transform: translateX(0); }
        }
        .auth-feat-icon {
          width: 36px;
          height: 36px;
          border-radius: 10px;
          background: rgba(255,255,255,.1);
          border: 1px solid rgba(255,255,255,.1);
          display: flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
        }
        .auth-feat-title {
          font-family: var(--font-dm-sans), sans-serif;
          font-size: 14px;
          font-weight: 600;
          color: white;
          margin-bottom: 2px;
          line-height: 1.4;
        }
        .auth-feat-desc {
          font-family: var(--font-dm-sans), sans-serif;
          font-size: 13px;
          color: rgba(255,255,255,.55);
          line-height: 1.5;
        }

        /* ── RIGHT PANEL ── */
        .auth-form-panel {
          background: #faf7f2;
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
          background: rgba(6, 64, 43, .03);
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
          font-family: var(--font-dm-sans), sans-serif;
          font-size: 14px;
          color: #6b6960;
          line-height: 1.5;
        }
        .auth-footer-links a {
          color: #06402B;
          font-weight: 600;
          text-decoration: none;
          transition: color 200ms ease-out;
        }
        .auth-footer-links a:hover { color: #054d33; }

        .auth-legal-links {
          margin-top: 16px;
          text-align: center;
          font-family: var(--font-dm-sans), sans-serif;
          font-size: 12px;
          color: #9e9a90;
          line-height: 1.5;
        }
        .auth-legal-links a {
          color: #6b6960;
          text-decoration: none;
          transition: color 200ms ease-out;
        }
        .auth-legal-links a:hover { color: #06402B; }

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
        <div className="auth-brand" aria-hidden="true">
          <div className="auth-brand-top">
            <Logo tone="cream" size="lg" />
          </div>

          <div className="auth-brand-middle">
            <div className="auth-eyebrow">Get started</div>
            <h1 className="auth-tagline">
              Your <em>personal AI</em>
              <br />
              starts here.
            </h1>

            <div className="auth-features" role="list">
              <div className="auth-feat" role="listitem">
                <div className="auth-feat-icon" aria-hidden="true">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#a8e6c6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
                    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  </svg>
                </div>
                <div>
                  <div className="auth-feat-title">Private by design</div>
                  <div className="auth-feat-desc">Your own isolated container. No shared data.</div>
                </div>
              </div>
              <div className="auth-feat" role="listitem">
                <div className="auth-feat-icon" aria-hidden="true">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#a8e6c6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="12" cy="12" r="10" />
                    <polyline points="12 6 12 12 16 14" />
                  </svg>
                </div>
                <div>
                  <div className="auth-feat-title">Always running</div>
                  <div className="auth-feat-desc">Works in the background, even when you&#39;re offline.</div>
                </div>
              </div>
              <div className="auth-feat" role="listitem">
                <div className="auth-feat-icon" aria-hidden="true">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#a8e6c6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 2L2 7l10 5 10-5-10-5z" />
                    <path d="M2 17l10 5 10-5" />
                    <path d="M2 12l10 5 10-5" />
                  </svg>
                </div>
                <div>
                  <div className="auth-feat-title">Extensible skills</div>
                  <div className="auth-feat-desc">Search, code, email, calendar -- and growing.</div>
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
              <Logo size="md" />
            </div>

            <SignUp fallbackRedirectUrl="/chat" signInFallbackRedirectUrl="/chat" />

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
