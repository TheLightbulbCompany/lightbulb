import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono, Source_Sans_3, DM_Sans, Lora } from "next/font/google";
import { ClerkProvider } from "@clerk/nextjs";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { DesktopAuthListener } from "@/components/DesktopAuthListener";
import { PostHogProvider } from "@/components/PostHogProvider";
import { SwrProvider } from "@/components/SwrProvider";
import { SupportProvider } from "@/components/support/SupportProvider";
import { Toaster } from "@/components/ui/sonner";
import { SITE_URL } from "@/lib/domain";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

// §05: the product face. Never below 400 — the manual forbids light weights.
const sourceSans = Source_Sans_3({
  variable: "--font-source-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
});

const dmSans = DM_Sans({
  variable: "--font-dm-sans",
  subsets: ["latin"],
  weight: ["400", "500", "700"],
});

const lora = Lora({
  variable: "--font-lora-serif",
  subsets: ["latin"],
  weight: ["400", "700"],
  style: ["normal", "italic"],
});

const SITE_TITLE = "Lightbulb AI — AI Agents for Autonomous Businesses";
const SITE_DESCRIPTION =
  "Point agents at the business you already run — sales, support, finance — and let it run itself. Always-on, cloud-isolated, and yours.";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: SITE_TITLE,
  description: SITE_DESCRIPTION,
  openGraph: {
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    url: SITE_URL,
    siteName: "Lightbulb AI",
    type: "website",
    images: [{ url: "/og-image.png", width: 1200, height: 630, alt: "Lightbulb AI" }],
  },
  twitter: {
    card: "summary_large_image",
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    images: ["/og-image.png"],
  },
};

// The landing room paints midnight; every other `.ld-page` route (/blog,
// /docs, /[seoSlug]) paints cream. INLINE and blocking so `data-lb-theme` is
// on <html> before the first stylesheet paints — a deferred script flashes.
// SCOPED to "/" because only the home page has a lamp to strike; writing the
// attribute anywhere else painted those routes midnight forever. The CSP is
// report-only today AND allows 'unsafe-inline' (next.config.ts); enforcing a
// nonce-based policy means giving this tag the same nonce.
const THEME_SCRIPT =
  "if(location.pathname==='/'){var t=null;try{t=localStorage.getItem('lightbulb-theme')}catch(e){}" +
  "document.documentElement.setAttribute('data-lb-theme',t==='light'?'light':'dark')}";

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
    <ClerkProvider>
      {/* suppressHydrationWarning: the script above writes an attribute onto
          <html> that the server-rendered markup does not carry. */}
      <html
        lang="en"
        suppressHydrationWarning
        className={`${sourceSans.variable} ${lora.variable} ${geistMono.variable} ${geistSans.variable} ${dmSans.variable}`}
      >
        <head>
          <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
        </head>
        <body className="antialiased">
          <ErrorBoundary>
            <SwrProvider>
              <PostHogProvider>
                <DesktopAuthListener />
                <SupportProvider>
                  {children}
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
