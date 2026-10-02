import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";

// Vendored from next/font/google (latin subset only — this site is English-only
// copy) so the build doesn't fetch Google Fonts over the network: the
// self-hosted runners flake on that fetch and the failure blocks every prod
// stack behind this job. Same families/weights/styles as before; see
// src/fonts/ for the source files.
const serif = localFont({
  variable: "--font-serif",
  src: [
    { path: "../fonts/instrument-serif-latin-400-normal.woff2", weight: "400", style: "normal" },
    { path: "../fonts/instrument-serif-latin-400-italic.woff2", weight: "400", style: "italic" },
  ],
});
const sans = localFont({
  variable: "--font-sans",
  src: [
    { path: "../fonts/inter-latin-400-500.woff2", weight: "400", style: "normal" },
    { path: "../fonts/inter-latin-400-500.woff2", weight: "500", style: "normal" },
  ],
});
const mono = localFont({
  variable: "--font-mono",
  src: [
    { path: "../fonts/jetbrains-mono-latin-400-500.woff2", weight: "400", style: "normal" },
    { path: "../fonts/jetbrains-mono-latin-400-500.woff2", weight: "500", style: "normal" },
  ],
});

const SITE = "https://about.thelightbulb.company";
const TITLE = "The Lightbulb Company — the business of ideas";
const DESCRIPTION =
  "A holding company for products that make people more capable. Lightbulb AI, GooseTown, Ventures, Physical AI.";

export const metadata: Metadata = {
  // The company site lives on about.; the apex is the product.
  metadataBase: new URL(SITE),
  title: TITLE,
  description: DESCRIPTION,
  alternates: { canonical: SITE },
  openGraph: {
    title: TITLE,
    description: DESCRIPTION,
    url: SITE,
    siteName: "The Lightbulb Company",
    type: "website",
  },
  // Adaptive favicon pack — see docs/brand/favicons.md. The SVG carries both
  // colourways and switches on prefers-color-scheme; the PNGs are the fallback.
  icons: {
    icon: [
      { url: "/favicon.svg", type: "image/svg+xml" },
      { url: "/favicon-32.png", sizes: "32x32" },
      { url: "/favicon-16.png", sizes: "16x16" },
      { url: "/icon-512.png", sizes: "512x512" },
    ],
    apple: "/apple-touch-icon.png",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${serif.variable} ${sans.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
