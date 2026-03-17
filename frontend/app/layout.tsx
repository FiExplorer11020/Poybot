import "@rainbow-me/rainbowkit/styles.css";
import "./globals.css";

import { ReactNode } from "react";

import { AppHeader } from "@/components/layout/AppHeader";
import { Providers } from "@/components/layout/Providers";
import { StatsSidebar } from "@/components/layout/StatsSidebar";
import { LiveSocketBridge } from "@/components/layout/LiveSocketBridge";

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="font-sans">
        <Providers>
          <div className="desktop-shell px-6 py-4">
            <LiveSocketBridge />
            <AppHeader />
            <div className="mt-4 grid grid-cols-[17%_83%] gap-4">
              <div className="sticky top-24 h-[calc(100vh-7.5rem)]">
                <StatsSidebar />
              </div>
              <div>{children}</div>
            </div>
          </div>
        </Providers>
      </body>
    </html>
  );
}
