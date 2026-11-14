import React from "react";
import Navbar from "../components/Navbar";
import Hero from "../components/Hero";
import Footer from "../components/Footer";

export default function LandingPage() {
  return (
    <div className="min-h-screen flex flex-col bg-app text-primary relative selection:bg-accent/20">
      {/* Hydrographic Chart Graticule (pure CSS hairline lat/lon grid) */}
      <div
        className="absolute inset-0 pointer-events-none opacity-40 dark:opacity-20 z-0"
        style={{
          backgroundImage: `
            linear-gradient(to right, var(--border-subtle) 1px, transparent 1px),
            linear-gradient(to bottom, var(--border-subtle) 1px, transparent 1px)
          `,
          backgroundSize: "64px 64px",
        }}
        aria-hidden="true"
      />

      <Navbar />
      <div className="pt-14 flex-1 flex flex-col">
        <Hero />
      </div>
      <Footer />
    </div>
  );
}
