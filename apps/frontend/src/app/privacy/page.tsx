import Link from "next/link";
import type { Metadata } from "next";

import { Logo } from "@/components/Logo";

export const metadata: Metadata = {
  title: "Privacy Policy — isol8",
  description: "How isol8 collects, uses, stores, and protects your personal information.",
};

export default function PrivacyPage() {
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
        <h1>Privacy Policy</h1>
        <p className="legal-meta">Effective date: March 24, 2026 &middot; Last updated: March 24, 2026</p>

        <h2>1. Introduction</h2>
        <p>Welcome to isol8. isol8 Inc. (&ldquo;isol8,&rdquo; &ldquo;we,&rdquo; &ldquo;us,&rdquo; or &ldquo;our&rdquo;) operates an AI agent hosting platform that provides users with dedicated, isolated containers for running AI agents. This Privacy Policy explains how we collect, use, store, and protect your personal information when you use our platform and services.</p>
        <p>By accessing or using isol8, you agree to the practices described in this Privacy Policy. If you do not agree, please discontinue use of our services.</p>

        <h2>2. Information We Collect</h2>
        <p>We collect the following categories of information:</p>
        <ul>
          <li><strong>Account Information:</strong> When you create an account, we collect your name, email address, and authentication credentials through our identity provider, Clerk. This includes your user ID and profile metadata.</li>
          <li><strong>Usage Data:</strong> We collect information about how you interact with the platform, including session activity, feature usage, API call frequency, and container resource consumption.</li>
          <li><strong>Agent Interaction Data:</strong> Messages you send to and receive from your AI agents, agent configuration settings, installed skills, and workspace files stored within your isolated environment.</li>
          <li><strong>Billing Information:</strong> Payment details are collected and processed by Stripe. We store your Stripe customer ID, subscription tier, and usage-based billing records. We do not directly store credit card numbers.</li>
          <li><strong>Technical Data:</strong> IP addresses, browser type, device information, and connection metadata required to maintain your WebSocket sessions.</li>
        </ul>

        <h2>3. How We Use Your Information</h2>
        <p>We use the information we collect to:</p>
        <ul>
          <li><strong>Provide and operate the service:</strong> Provision your dedicated containers, maintain WebSocket connections, execute agent interactions, and deliver platform functionality.</li>
          <li><strong>Process billing:</strong> Calculate usage-based charges, manage subscriptions, process payments through Stripe, and maintain accurate billing records.</li>
          <li><strong>Improve the platform:</strong> Analyze aggregate usage patterns to improve performance, reliability, and user experience. We use anonymized and aggregated data for this purpose.</li>
          <li><strong>Ensure security:</strong> Detect and prevent fraud, abuse, and unauthorized access to your account and containers.</li>
          <li><strong>Communicate with you:</strong> Send service-related notifications, respond to support requests, and provide important updates about your account or our policies.</li>
        </ul>

        <h2>4. Data Storage &amp; Security</h2>
        <p>We take the security of your data seriously and employ industry-standard measures to protect it:</p>
        <ul>
          <li><strong>Infrastructure:</strong> Our platform runs on Amazon Web Services (AWS) infrastructure, leveraging their enterprise-grade physical and network security controls.</li>
          <li><strong>Encryption at rest:</strong> All stored data, including database contents, file system volumes, and secrets, is encrypted at rest using AWS KMS-managed encryption keys.</li>
          <li><strong>Encryption in transit:</strong> All data transmitted between your browser and our servers, and between internal services, is encrypted using TLS.</li>
          <li><strong>Per-user isolation:</strong> Each user receives a dedicated, isolated container environment on AWS ECS Fargate. Your agent workspaces are stored on dedicated EFS access points, ensuring complete separation from other users&rsquo; data.</li>
          <li><strong>Secrets management:</strong> Sensitive credentials and API keys are stored using AWS Secrets Manager with envelope encryption.</li>
        </ul>

        <h2>5. AI &amp; Agent Data</h2>
        <p>Your interactions with AI agents are treated with special care:</p>
        <ul>
          <li><strong>Conversation storage:</strong> Agent conversations are stored within your per-user isolated workspace. Only you (and authorized platform systems required for operation) can access this data.</li>
          <li><strong>No model training:</strong> Your agent conversations, workspace files, and interaction data are <strong>not</strong> used to train, fine-tune, or improve any AI or machine learning models. Your data remains yours.</li>
          <li><strong>User control:</strong> You retain full control over your agent data. You can view, manage, and delete agent memory, conversation history, and workspace files at any time through the platform interface.</li>
          <li><strong>LLM inference:</strong> When your agents use AI models, prompts are sent to the inference provider (e.g., AWS Bedrock) for real-time processing only. These providers process requests under their enterprise terms, which prohibit using your data for model training.</li>
        </ul>

        <h2>6. Third-Party Services</h2>
        <p>We rely on the following third-party services to operate our platform. Each has its own privacy policy governing data handling:</p>
        <ul>
          <li><strong>Clerk</strong> — Authentication, user identity management, and session handling.</li>
          <li><strong>Stripe</strong> — Payment processing, subscription management, and usage-based billing.</li>
          <li><strong>Amazon Web Services (AWS)</strong> — Cloud infrastructure, container orchestration, file storage, and secrets management.</li>
          <li><strong>AWS Bedrock</strong> — Large language model inference for AI agent capabilities, operating under AWS&rsquo;s enterprise data processing terms.</li>
        </ul>
        <p>We do not sell your personal information to any third party.</p>

        <h2>7. Data Retention</h2>
        <p>We retain your personal information for as long as your account remains active and as needed to provide our services. Specifically:</p>
        <ul>
          <li><strong>Active accounts:</strong> Your data, including agent workspaces, conversation history, and configuration, is maintained for the duration of your account.</li>
          <li><strong>Account deletion:</strong> When you delete your account, we initiate deletion of your personal data, agent workspaces, container resources, and associated records. This process is completed within 30 days, except where retention is required by law.</li>
          <li><strong>Billing records:</strong> Transaction and billing records may be retained for up to 7 years as required for tax, legal, and accounting compliance.</li>
          <li><strong>Aggregated data:</strong> Anonymized, aggregated usage statistics that cannot identify individual users may be retained indefinitely for analytics purposes.</li>
        </ul>

        <h2>8. Your Rights</h2>
        <p>Depending on your jurisdiction, you may have the following rights regarding your personal data:</p>
        <ul>
          <li><strong>Access:</strong> Request a copy of the personal data we hold about you.</li>
          <li><strong>Correction:</strong> Request correction of any inaccurate or incomplete personal data.</li>
          <li><strong>Deletion:</strong> Request deletion of your personal data and account, subject to legal retention requirements.</li>
          <li><strong>Data export:</strong> Request a portable copy of your data, including agent configurations and workspace files.</li>
          <li><strong>Restriction:</strong> Request restriction of processing of your personal data in certain circumstances.</li>
          <li><strong>Objection:</strong> Object to processing of your personal data for specific purposes.</li>
        </ul>
        <p>To exercise any of these rights, please contact us at <a href="mailto:privacy@isol8.co">privacy@isol8.co</a>. We will respond to your request within 30 days.</p>

        <h2>9. Cookies &amp; Tracking</h2>
        <p>We use a minimal set of cookies and similar technologies:</p>
        <ul>
          <li><strong>Authentication tokens:</strong> Session cookies managed by Clerk to keep you signed in and secure your account.</li>
          <li><strong>Essential cookies:</strong> Cookies strictly necessary for platform functionality, such as connection state and user preferences.</li>
        </ul>
        <p>We do <strong>not</strong> use third-party advertising trackers, social media tracking pixels, or cross-site analytics cookies. We do not participate in ad networks or sell tracking data.</p>

        <h2>10. Children&rsquo;s Privacy</h2>
        <p>isol8 is not intended for use by children under the age of 13. We do not knowingly collect personal information from children under 13. If we become aware that we have inadvertently collected data from a child under 13, we will take steps to delete that information promptly. If you believe a child under 13 has provided us with personal information, please contact us at <a href="mailto:privacy@isol8.co">privacy@isol8.co</a>.</p>

        <h2>11. Changes to This Policy</h2>
        <p>We may update this Privacy Policy from time to time to reflect changes in our practices, technologies, legal requirements, or other factors. When we make material changes, we will notify you by updating the &ldquo;Last updated&rdquo; date at the top of this page and, where appropriate, provide additional notice via email or an in-app notification.</p>
        <p>We encourage you to review this Privacy Policy periodically. Your continued use of isol8 after any changes constitutes your acceptance of the updated policy.</p>

        <h2>12. Contact Us</h2>
        <p>If you have any questions, concerns, or requests regarding this Privacy Policy or our data practices, please contact us:</p>
        <ul>
          <li><strong>Email:</strong> <a href="mailto:privacy@isol8.co">privacy@isol8.co</a></li>
          <li><strong>Company:</strong> isol8 Inc.</li>
        </ul>
        <p>We are committed to resolving any privacy concerns promptly and transparently.</p>
      </main>

      <footer className="legal-footer">
        <p>&copy; {new Date().getFullYear()} isol8 Inc. All rights reserved.</p>
        <nav className="legal-footer-links">
          <Link href="/terms">Terms of Service</Link>
          <Link href="/support">Support</Link>
        </nav>
      </footer>
    </div>
  );
}
