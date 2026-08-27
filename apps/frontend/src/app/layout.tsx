import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono, Host_Grotesk, DM_Sans, Lora, Epilogue } from "next/font/google";
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

const hostGrotesk = Host_Grotesk({
  variable: "--font-host-grotesk",
  subsets: ["latin"],
  weight: ["400", "700"],
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

const epilogue = Epilogue({
  variable: "--font-epilogue",
  subsets: ["latin"],
  weight: ["300", "400", "500"],
});

const SITE_TITLE = "isol8 — AI Agents for Autonomous Businesses";
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
    siteName: "isol8",
    type: "website",
    images: [{ url: "/og-image.png", width: 1200, height: 630, alt: "isol8" }],
  },
  twitter: {
    card: "summary_large_image",
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    images: ["/og-image.png"],
  },
};

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
      <html lang="en">
        <body
          className={`${geistSans.variable} ${geistMono.variable} ${hostGrotesk.variable} ${dmSans.variable} ${lora.variable} ${epilogue.variable} antialiased`}
        >
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
