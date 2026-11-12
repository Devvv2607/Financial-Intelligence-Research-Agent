import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

import { TopBar } from "@/components/shell/top-bar";

import { Providers } from "./providers";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Financial Research Agent",
  description: "Agentic research over SEC filings and financial news",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} dark h-full antialiased`}
    >
      <body className="flex h-dvh flex-col overflow-hidden">
        <Providers>
          <TopBar />
          <div className="flex min-h-0 flex-1 flex-col">{children}</div>
        </Providers>
      </body>
    </html>
  );
}
