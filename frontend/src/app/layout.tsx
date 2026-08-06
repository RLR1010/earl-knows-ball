import type { Metadata } from "next";
import "./globals.css";
import SiteChrome from "./SiteChrome";

export const metadata: Metadata = {
  title: {
    default: "Earl Knows Ball — AI-Powered Sports Handicapping & Picks",
    template: "%s | Earl Knows Ball",
  },
  description:
    "Earl Knows Ball is the ultimate AI-powered sports handicapping tool for NFL, MLB, and NBA — game picks with probabilities, betting lines, trends, and a chat handicapper that explains every call.",
  keywords: [
    "sports betting",
    "NFL picks",
    "MLB picks",
    "NBA picks",
    "AI handicapper",
    "betting odds",
    "spreads",
    "over under",
    "sports predictions",
    "Earl Knows Ball",
  ],
  metadataBase: new URL("https://earlknowsball.com"),
  alternates: {
    canonical: "/",
  },
  robots: {
    index: true,
    follow: true,
  },
  openGraph: {
    title: "Earl Knows Ball — AI-Powered Sports Handicapping & Picks",
    description:
      "The ultimate AI sports handicapping tool for NFL, MLB, and NBA — picks, odds, trends, and a chat handicapper.",
    url: "https://earlknowsball.com/",
    siteName: "Earl Knows Ball",
    type: "website",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <head>
        <link rel="icon" type="image/png" href="/earl-icon.png" />
        <link rel="apple-touch-icon" href="/earl-icon.png" />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Oswald:wght@400;500;600;700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="min-h-screen flex flex-col bg-[#0a0a0f] text-gray-200 antialiased">
        <SiteChrome>{children}</SiteChrome>
      </body>
    </html>
  );
}
