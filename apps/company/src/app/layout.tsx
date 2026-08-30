import type { Metadata } from "next";
import { Instrument_Serif, Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";

const serif = Instrument_Serif({ variable: "--font-serif", subsets: ["latin"], weight: "400", style: ["normal", "italic"] });
const sans = Inter({ variable: "--font-sans", subsets: ["latin"], weight: ["400", "500"] });
const mono = JetBrains_Mono({ variable: "--font-mono", subsets: ["latin"], weight: ["400", "500"] });

const TITLE = "The Lightbulb Company — the business of ideas";
const DESCRIPTION =
  "A holding company for products that make people more capable. Lightbulb AI, GooseTown, Ventures, Physical AI.";

export const metadata: Metadata = {
  metadataBase: new URL("https://thelightbulb.company"),
  title: TITLE,
  description: DESCRIPTION,
  openGraph: {
    title: TITLE,
    description: DESCRIPTION,
    url: "https://thelightbulb.company",
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
