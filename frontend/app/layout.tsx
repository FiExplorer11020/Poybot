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
          <div className="mx-auto w-full max-w-[1680px] px-3 py-3 sm:px-4 lg:px-6">
            <LiveSocketBridge />
            <AppHeader />
            <div className="mt-4 grid gap-4 xl:grid-cols-[280px_minmax(0,1fr)]">
              <div className="xl:sticky xl:top-24 xl:h-[calc(100vh-7.5rem)]">
                <StatsSidebar />
              </div>
              <main className="min-w-0">{children}</main>
            </div>
          </div>
        </Providers>
      </body>
    </html>
  );
}
