import type { Metadata, Viewport } from "next";
import { Geist_Mono, Source_Sans_3, Lora } from "next/font/google";
import { ClerkProvider } from "@clerk/nextjs";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { DeployRefresh } from "@/components/DeployRefresh";
import { DesktopAuthListener } from "@/components/DesktopAuthListener";
import { ExplicitSharingNotice, LiveSharingOffer } from "@/components/desktop/ExplicitSharingNotice";
import { InAppNavTracker } from "@/components/legal/BackButton";
import { PostHogProvider } from "@/components/PostHogProvider";
import { SwrProvider } from "@/components/SwrProvider";
import { SupportProvider } from "@/components/support/SupportProvider";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  clerkAppearance,
  clerkLocalization,
  clerkTaskUrls,
} from "@/lib/clerkAppearance";
import { SITE_URL } from "@/lib/domain";
import "./globals.css";

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

// §05: the product face. Never below 400 — the manual forbids light weights.
const sourceSans = Source_Sans_3({
  variable: "--font-source-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  display: "swap",
});

const lora = Lora({
  variable: "--font-lora-serif",
  subsets: ["latin"],
  // Variable font: the manual sets Lora at 400/500/600 and static 400/700 snapped
  // every 500 and 600 to 700 in production.
  style: ["normal", "italic"],
});


const SITE_TITLE = "Lightbulb — AI Agents for Autonomous Businesses";
const SITE_DESCRIPTION =
  "Run your company with persistent AI agents, cloud workspaces, and an agent marketplace for autonomous business operations.";
const SOCIAL_IMAGE = "/og-lightbulb-v2.png";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: SITE_TITLE,
  description: SITE_DESCRIPTION,
  openGraph: {
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    url: SITE_URL,
    siteName: "Lightbulb",
    type: "website",
    images: [{ url: SOCIAL_IMAGE, width: 1200, height: 630, alt: "Lightbulb" }]
  },
  twitter: {
    card: "summary_large_image",
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    images: [SOCIAL_IMAGE]
  },
};

// A stored dark choice must be on <html> BEFORE the first stylesheet paints —
// a deferred script flashes light then dark. Light is the default, so with
// nothing stored the attribute is simply absent and the server markup already
// matches. Only `.ld-page` reads the attribute (landing, /docs, /blog,
// /[seoSlug]); it cannot reach /chat or admin, so the script runs everywhere.
// The nav toggle (Navbar.tsx → lampStore) writes the same attribute + key.
const THEME_SCRIPT =
  "try{if(localStorage.getItem('lightbulb-theme')==='dark')" +
  "document.documentElement.setAttribute('data-lb-theme','dark')}catch(e){}";

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <ClerkProvider
      appearance={clerkAppearance}
      localization={clerkLocalization}
      taskUrls={clerkTaskUrls}
    >
      {/* suppressHydrationWarning: THEME_SCRIPT writes data-lb-theme onto
          <html> for returning dark-mode visitors, which the server markup
          does not carry. */}
      <html
        lang="en"
        suppressHydrationWarning
        className={`${sourceSans.variable} ${lora.variable} ${geistMono.variable}`}
      >
        <head>
          <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
        </head>
        <body className="antialiased">
          <ErrorBoundary>
            <SwrProvider>
              <PostHogProvider>
                <DesktopAuthListener />
                <ExplicitSharingNotice />
                <LiveSharingOffer />
                <DeployRefresh />
                <SupportProvider>
                  {/* One provider for the whole app so the tooltip skip-delay
                      grace is shared: after the first tooltip, neighbouring
                      ones open instantly instead of re-waiting. */}
                  <InAppNavTracker />
                  <TooltipProvider>{children}</TooltipProvider>
                  {/* Toaster INSIDE SupportProvider so a toast's <ReportButton/>
                      (a custom-JSX sonner action) can resolve useSupport(). */}
                  <Toaster />
                </SupportProvider>
              </PostHogProvider>
            </SwrProvider>
          </ErrorBoundary>
        </body>
      </html>
    </ClerkProvider>
  );
}
