import Link from "next/link";
import type { Metadata } from "next";

import { Logo } from "@/components/Logo";

export const metadata: Metadata = {
  title: "Terms of Service — isol8",
  description: "Terms of Service for the isol8 AI agent platform.",
};

export default function TermsPage() {
  return (
    <div className="legal-page">
      <header className="legal-topbar">
        <Logo size="md" />
        <Link href="/" className="legal-btn-back">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <path d="M10 12L6 8L10 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Back
        </Link>
      </header>

      <hr className="legal-divider" />

      <main className="legal-content">
        <h1>Terms of Service</h1>
        <p className="legal-meta">Effective date: March 24, 2026 &middot; Last updated: March 24, 2026</p>

        <h2>1. Acceptance of Terms</h2>
        <p>By accessing or using the isol8 platform (&ldquo;Service&rdquo;), operated by isol8 Inc. (&ldquo;isol8,&rdquo; &ldquo;we,&rdquo; &ldquo;us,&rdquo; or &ldquo;our&rdquo;), you (&ldquo;User&rdquo; or &ldquo;you&rdquo;) agree to be bound by these Terms of Service (&ldquo;Terms&rdquo;). If you do not agree, you may not use the Service.</p>
        <p>These Terms constitute a legally binding agreement between you and isol8 Inc. Your continued use of the Service following the posting of any changes to these Terms constitutes acceptance of those changes.</p>

        <h2>2. Description of Service</h2>
        <p>isol8 is an AI agent hosting platform that provides each user with a dedicated, isolated compute container running persistent AI agents. Key features of the Service include:</p>
        <ul>
          <li><strong>Per-user containers:</strong> Each user receives their own isolated ECS Fargate container with a persistent workspace for agent configuration, data, and state.</li>
          <li><strong>Persistent agents:</strong> AI agents run continuously within your container, maintaining context and state across sessions.</li>
          <li><strong>Real-time interaction:</strong> WebSocket-based communication enables real-time streaming of agent responses and tool use.</li>
          <li><strong>Multi-channel support:</strong> Agents can be connected to third-party messaging platforms such as Telegram, Discord, and WhatsApp.</li>
          <li><strong>LLM inference:</strong> The platform provides access to large language models via AWS Bedrock for agent reasoning and generation.</li>
        </ul>

        <h2>3. Account Registration</h2>
        <p>To use the Service, you must create an account through our authentication provider, Clerk. By registering, you agree to:</p>
        <ul>
          <li>Provide accurate, current, and complete information during registration.</li>
          <li>Maintain the security of your account credentials and not share them with any third party.</li>
          <li>Promptly notify isol8 of any unauthorized use of your account.</li>
          <li>Accept responsibility for all activities that occur under your account.</li>
        </ul>
        <p>You must be at least 18 years of age to create an account. isol8 reserves the right to refuse registration or terminate accounts at its discretion.</p>

        <h2>4. Subscription &amp; Billing</h2>
        <p>The Service offers the following subscription tiers:</p>
        <table className="legal-tier-table">
          <thead>
            <tr><th>Plan</th><th>Monthly Budget</th><th>Details</th></tr>
          </thead>
          <tbody>
            <tr><td>Free</td><td>$2</td><td>No subscription required. Usage capped at $2 of compute per month.</td></tr>
            <tr><td>Starter</td><td>$25</td><td>Fixed monthly subscription plus usage-based metering.</td></tr>
            <tr><td>Pro</td><td>$75</td><td>Fixed monthly subscription plus usage-based metering.</td></tr>
          </tbody>
        </table>
        <p>Billing is processed through Stripe. By subscribing to a paid plan, you authorize isol8 to charge your payment method on a recurring basis.</p>
        <p><strong>Usage-based metering:</strong> Compute and inference usage is tracked and billed based on actual consumption. A markup of 1.4x is applied to the base cost of model inference and compute resources to cover platform infrastructure and operational costs.</p>
        <p>You may upgrade, downgrade, or cancel your subscription at any time through the billing portal. Downgrades and cancellations take effect at the end of the current billing period. Refunds are not provided for partial billing periods.</p>

        <h2>5. Acceptable Use</h2>
        <p>You agree not to use the Service to:</p>
        <ul>
          <li>Engage in any illegal activity or facilitate unlawful conduct.</li>
          <li>Abuse compute resources, including but not limited to cryptocurrency mining, denial-of-service attacks, or resource-exhaustion attacks.</li>
          <li>Circumvent or attempt to circumvent usage limits, billing mechanisms, or rate-limiting controls.</li>
          <li>Deploy agents that engage in harassment, generate harmful or deceptive content, or distribute malware.</li>
          <li>Attempt to access other users&rsquo; containers, workspaces, or data.</li>
          <li>Reverse engineer, decompile, or disassemble any part of the Service.</li>
          <li>Use the Service in any manner that could damage, disable, or impair the platform&rsquo;s infrastructure.</li>
        </ul>
        <p>isol8 reserves the right to suspend or terminate your account immediately if we determine, in our sole discretion, that you have violated this section.</p>

        <h2>6. AI Agent Conduct</h2>
        <p>You are solely responsible for the actions, outputs, and behavior of the AI agents you configure and deploy on the platform. This includes:</p>
        <ul>
          <li><strong>Compliance:</strong> Your agents must comply with all applicable laws, regulations, and third-party API terms of service.</li>
          <li><strong>Supervision:</strong> You must monitor your agents and ensure they operate within the bounds of their intended purpose.</li>
          <li><strong>Financial transactions:</strong> Agents must not execute autonomous financial transactions without appropriate human-in-the-loop safeguards and explicit user confirmation mechanisms.</li>
          <li><strong>Third-party interactions:</strong> When your agents interact with external services, you are responsible for ensuring those interactions comply with the respective service&rsquo;s terms of use.</li>
        </ul>
        <p>isol8 does not endorse, verify, or assume liability for any content generated by or actions taken by your AI agents.</p>

        <h2>7. Intellectual Property</h2>
        <p><strong>Platform ownership:</strong> The isol8 platform, including its software, design, documentation, branding, and infrastructure, is the exclusive property of isol8 Inc. and is protected by intellectual property laws.</p>
        <p><strong>User content:</strong> You retain all rights to the content you upload, configure, or input into the Service, including agent configurations, prompts, and data files.</p>
        <p><strong>Agent outputs:</strong> Content generated by your AI agents belongs to you, subject to the terms of the underlying model providers. isol8 does not claim ownership of agent-generated outputs.</p>

        <h2>8. Data &amp; Privacy</h2>
        <p>Your privacy is important to us. Our collection, use, and protection of your personal data is governed by our <Link href="/privacy">Privacy Policy</Link>, which is incorporated into these Terms by reference.</p>
        <p>By using the Service, you acknowledge that your data, including agent configurations and conversation history, is stored in your isolated container workspace and in our database systems. We employ encryption at rest and in transit to protect your data.</p>

        <h2>9. Third-Party Services</h2>
        <p>The Service integrates with and relies upon the following third-party services, each of which is subject to its own terms and conditions:</p>
        <ul>
          <li><strong>OpenClaw:</strong> Open-source AI agent framework powering your containers.</li>
          <li><strong>AWS Bedrock:</strong> Large language model inference provider.</li>
          <li><strong>Clerk:</strong> Authentication and user management.</li>
          <li><strong>Stripe:</strong> Payment processing and subscription management.</li>
        </ul>
        <p>isol8 is not responsible for the availability, accuracy, or performance of third-party services.</p>

        <h2>10. Service Availability</h2>
        <p>isol8 strives to maintain high availability of the Service but provides it on a &ldquo;best effort&rdquo; basis. We do not guarantee uninterrupted or error-free operation.</p>
        <ul>
          <li>Scheduled maintenance windows may result in temporary service interruptions. We will endeavor to provide reasonable advance notice.</li>
          <li>Unplanned outages may occur due to infrastructure failures, upstream provider issues, or other unforeseen circumstances.</li>
          <li>isol8 does not provide service-level agreements (SLAs) for uptime unless separately agreed upon in writing.</li>
        </ul>

        <h2>11. Limitation of Liability</h2>
        <p>TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, ISOL8 INC., ITS OFFICERS, DIRECTORS, EMPLOYEES, AND AGENTS SHALL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, INCLUDING BUT NOT LIMITED TO LOSS OF PROFITS, DATA, USE, OR GOODWILL, ARISING OUT OF OR RELATED TO YOUR USE OF THE SERVICE.</p>
        <p>IN NO EVENT SHALL ISOL8&rsquo;S TOTAL LIABILITY TO YOU FOR ALL CLAIMS ARISING OUT OF OR RELATING TO THESE TERMS OR THE SERVICE EXCEED THE AMOUNT YOU PAID TO ISOL8 IN THE TWELVE (12) MONTHS PRECEDING THE CLAIM.</p>

        <h2>12. Termination</h2>
        <p><strong>By you:</strong> You may delete your account and terminate your use of the Service at any time. Upon account deletion, your container, workspace data, and agent configurations will be permanently destroyed after a 30-day grace period.</p>
        <p><strong>By isol8:</strong> We may suspend or terminate your account immediately and without prior notice if you violate these Terms, engage in fraudulent activity, or pose a risk to the platform or other users.</p>
        <p>Upon termination, your right to use the Service ceases immediately. Sections 7, 8, 11, and 13 survive termination.</p>

        <h2>13. Governing Law</h2>
        <p>These Terms shall be governed by and construed in accordance with the laws of the State of Delaware, United States, without regard to its conflict-of-law provisions. Any disputes arising under these Terms shall be subject to the exclusive jurisdiction of the state and federal courts located in Delaware.</p>

        <h2>14. Changes to Terms</h2>
        <p>isol8 reserves the right to modify these Terms at any time. When we make material changes, we will update the &ldquo;Last updated&rdquo; date at the top of this page and notify you via email or an in-app notification at least 30 days before the changes take effect.</p>

        <h2>15. Contact</h2>
        <p>If you have any questions about these Terms, please contact us at:</p>
        <p><strong>isol8 Inc.</strong><br />Email: <a href="mailto:legal@isol8.co">legal@isol8.co</a></p>
      </main>

      <footer className="legal-footer">
        <p>&copy; {new Date().getFullYear()} isol8 Inc. All rights reserved.</p>
        <nav className="legal-footer-links">
          <Link href="/privacy">Privacy Policy</Link>
          <Link href="/support">Support</Link>
        </nav>
      </footer>
    </div>
  );
}
