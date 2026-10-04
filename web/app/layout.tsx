import type { Metadata } from "next";
import { Bricolage_Grotesque, Source_Sans_3 } from "next/font/google";
import "./globals.css";

const bricolage = Bricolage_Grotesque({ variable: "--font-bricolage", subsets: ["latin"] });
const sourceSans = Source_Sans_3({ variable: "--font-source", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Glow Studio: book by talking to Shanaya",
  description:
    "See our services and today's schedule, then book, move or cancel an appointment by talking to Shanaya, our AI receptionist.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${bricolage.variable} ${sourceSans.variable} h-full antialiased`}>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}