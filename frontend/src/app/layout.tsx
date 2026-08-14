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
        {/* Google tag (gtag.js) */}
        <script
          async
          src="https://www.googletagmanager.com/gtag/js?id=G-8H21CC0XRP"
        />
        <script
          dangerouslySetInnerHTML={{
            __html: `
              window.dataLayer = window.dataLayer || [];
              function gtag(){dataLayer.push(arguments);}
              gtag('js', new Date());
              gtag('config', 'G-8H21CC0XRP');
            `,
          }}
        />
        <link rel="icon" type="image/png" href="/earl-icon.png" />
        <link rel="apple-touch-icon" href="/earl-icon.png" />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Oswald:wght@400;500;600;700&display=swap"
          rel="stylesheet"
        />
        {/* Make React clickable elements (divs w/ onClick etc.) show a pointer cursor */}
        <script
          dangerouslySetInnerHTML={{
            __html: `
              (function () {
                var KEY = '__reactProps$';
                function hasClickHandler(node) {
                  for (var k in node) {
                    if (k.indexOf(KEY) === 0 && node[k] &&
                        (node[k].onClick || node[k].onMouseDown || node[k].onPointerDown)) {
                      return true;
                    }
                  }
                  return false;
                }
                function apply() {
                  var all = document.querySelectorAll('*');
                  for (var i = 0; i < all.length; i++) {
                    var el = all[i];
                    var cs = getComputedStyle(el);
                    if (cs.cursor === 'pointer') continue;
                    if (hasClickHandler(el)) el.style.cursor = 'pointer';
                  }
                }
                window.addEventListener('load', apply);
                // Re-run periodically to catch newly rendered nodes (SPA navigation / mounts)
                setInterval(apply, 1500);
              })();
            `,
          }}
        />
      </head>
      <body className="min-h-screen flex flex-col bg-[#0a0a0f] text-gray-200 antialiased">
        <SiteChrome>{children}</SiteChrome>
      </body>
    </html>
  );
}
