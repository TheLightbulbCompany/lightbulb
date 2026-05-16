"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

import { Logo } from "@/components/Logo";

export default function SupportPage() {
  const router = useRouter();

  return (
    <div className="legal-page">
      <header className="legal-topbar">
        <Logo size="md" />
        <button onClick={() => router.back()} className="legal-btn-back" type="button">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <path d="M10 12L6 8L10 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Back
        </button>
      </header>

      <hr className="legal-divider" />

      <main className="support-content">
        <div className="support-header">
          <h1>Support</h1>
          <p>We&rsquo;re here to help. Send us a message and we&rsquo;ll get back to you.</p>
        </div>

        <form className="support-form" action="mailto:support@isol8.co" method="POST" encType="text/plain">
          <div className="support-field">
            <label htmlFor="name">Name</label>
            <input type="text" id="name" name="name" required autoComplete="name" placeholder="Your name" />
          </div>

          <div className="support-field">
            <label htmlFor="email">Email</label>
            <input type="email" id="email" name="email" required autoComplete="email" placeholder="you@example.com" />
          </div>

          <div className="support-field">
            <label htmlFor="subject">Subject</label>
            <select id="subject" name="subject" required defaultValue="">
              <option value="" disabled>Select a topic</option>
              <option value="Bug report">Bug report</option>
              <option value="Feature request">Feature request</option>
              <option value="Billing question">Billing question</option>
              <option value="Account issue">Account issue</option>
              <option value="GooseTown">GooseTown</option>
              <option value="Other">Other</option>
            </select>
          </div>

          <div className="support-field">
            <label htmlFor="message">Message</label>
            <textarea id="message" name="message" required placeholder="Describe how we can help..." />
          </div>

          <button type="submit" className="support-submit">Send message</button>
        </form>

        <div className="support-direct">
          <p>You can also reach us directly at <a href="mailto:support@isol8.co">support@isol8.co</a></p>
        </div>
      </main>

      <footer className="legal-footer">
        <p>&copy; {new Date().getFullYear()} isol8 Inc. All rights reserved.</p>
        <nav className="legal-footer-links">
          <Link href="/privacy">Privacy Policy</Link>
          <Link href="/terms">Terms</Link>
        </nav>
      </footer>
    </div>
  );
}
