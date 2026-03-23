import "./globals.css";

import { ReactNode } from "react";
import { IBM_Plex_Sans, JetBrains_Mono } from "next/font/google";
import { Toaster } from "sonner";

import { AppHeader } from "@/components/layout/AppHeader";
import { LiveSocketBridge } from "@/components/layout/LiveSocketBridge";
import { Providers } from "@/components/layout/Providers";
import { StatsSidebar } from "@/components/layout/StatsSidebar";

const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-plex-sans",
});

const jetBrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-jetbrains-mono",
});

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${plexSans.variable} ${jetBrainsMono.variable} dark`}>
      <body className="overflow-hidden font-sans">
        <Providers>
          <LiveSocketBridge />
          <div className="mx-auto flex h-screen w-full max-w-[1840px] bg-transparent">
            <div className="hidden w-[240px] shrink-0 lg:block">
              <StatsSidebar />
            </div>

            <div className="flex min-w-0 flex-1 flex-col">
              <div className="border-b border-white/8 px-4 py-4 lg:hidden">
                <StatsSidebar mobile />
              </div>
              <AppHeader />
              <main className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6 lg:px-8">{children}</main>
            </div>
          </div>
          <Toaster theme="dark" position="bottom-right" />
        </Providers>
      </body>
    </html>
  );
}
