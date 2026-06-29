import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono, Host_Grotesk, DM_Sans, Lora, Press_Start_2P, Epilogue } from "next/font/google";
import { ClerkProvider } from "@clerk/nextjs";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { DesktopAuthListener } from "@/components/DesktopAuthListener";
import { PostHogProvider } from "@/components/PostHogProvider";
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

const pressStart2P = Press_Start_2P({
  variable: "--font-press-start",
  subsets: ["latin"],
  weight: ["400"],
});

const epilogue = Epilogue({
  variable: "--font-epilogue",
  subsets: ["latin"],
  weight: ["300", "400", "500"],
});

const SITE_TITLE = "isol8 - Your AI Pod";
const SITE_DESCRIPTION =
  "Your own isolated AI pod with persistent memory, custom personality, and skills that grow with you.";

export const metadata: Metadata = {
  metadataBase: new URL("https://isol8.co"),
  title: SITE_TITLE,
  description: SITE_DESCRIPTION,
  openGraph: {
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    url: "https://isol8.co",
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
          className={`${geistSans.variable} ${geistMono.variable} ${hostGrotesk.variable} ${dmSans.variable} ${lora.variable} ${pressStart2P.variable} ${epilogue.variable} antialiased`}
        >
          <ErrorBoundary>
            <PostHogProvider>
              <DesktopAuthListener />
              {children}
            </PostHogProvider>
          </ErrorBoundary>
        </body>
      </html>
    </ClerkProvider>
  );
}
