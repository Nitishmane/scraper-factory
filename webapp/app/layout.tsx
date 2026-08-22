import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Top 10 shows & movies — Scraper Factory",
  description:
    "The top 10 shows and top 10 movies airing this week across Philo, YouTube TV, and Sling — scraped, ranked, and one click from Record.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
