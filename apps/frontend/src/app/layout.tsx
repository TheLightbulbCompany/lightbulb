import type { Metadata, Viewport } from "next";
import { Geist_Mono } from "next/font/google";
import localFont from "next/font/local";
import { ClerkProvider } from "@clerk/nextjs";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { DeployRefresh } from "@/components/DeployRefresh";
import { InAppNavTracker } from "@/components/legal/BackButton";
import { PostHogProvider } from "@/components/PostHogProvider";
import { SwrProvider } from "@/components/SwrProvider";
import { VercelAnalytics } from "@/components/VercelAnalytics";
import { SupportProvider } from "@/components/support/SupportProvider";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { clerkAppearance } from "@/lib/clerkAppearance";
import { SITE_URL } from "@/lib/domain";
import { OG_IMAGE, SITE_DESCRIPTION, SITE_TITLE } from "@/lib/seo";
import "./globals.css";

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

const satoshi = localFont({
  src: "../fonts/satoshi-variable.woff2",
  variable: "--font-satoshi",
  weight: "300 900",
  style: "normal",
  display: "swap",
  fallback: ["Avenir Next", "Segoe UI", "sans-serif"],
});


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
    images: [{ url: OG_IMAGE, width: 1200, height: 630, alt: "Lightbulb" }]
  },
  twitter: {
    card: "summary_large_image",
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    images: [OG_IMAGE]
  },
};

// A stored dark choice must be on <html> BEFORE the first stylesheet paints —
// a deferred script flashes light then dark. Light is the default, so with
// nothing stored the attribute is simply absent and the server markup already
// matches. Only `.ld-page` reads the attribute (landing, /docs, /blog,
// /compare); it cannot reach /chat or admin, so the script runs everywhere.
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
    <ClerkProvider appearance={clerkAppearance}>
      {/* suppressHydrationWarning: THEME_SCRIPT writes data-lb-theme onto
          <html> for returning dark-mode visitors, which the server markup
          does not carry. */}
      <html
        lang="en"
        suppressHydrationWarning
        className={`${satoshi.variable} ${geistMono.variable}`}
      >
        <head>
          <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
        </head>
        <body className="antialiased">
          {/* The boundary sits inside PostHogProvider so PostHog still loads when a page throws, and the
              caught error reaches it (ErrorBoundary.componentDidCatch). */}
          <SwrProvider>
            <PostHogProvider>
              <ErrorBoundary>
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
              </ErrorBoundary>
            </PostHogProvider>
          </SwrProvider>
          <VercelAnalytics />
        </body>
      </html>
    </ClerkProvider>
  );
}
