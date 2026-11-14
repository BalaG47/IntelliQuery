import React from "react";
import SearchBar from "./Startbtn";
import globevid from "../assets/abcd.mp4";

export default function Hero() {
  return (
    <main className="flex flex-col items-center justify-center flex-1 px-4 sm:px-6 py-12 text-center text-primary relative z-10">
      
      {/* Instrument Bezel with Spinning Globe */}
      <div className="relative flex items-center justify-center">
        {/* Outer Circular Gauge / Instrument Bezel (SVG ticks) */}
        <div className="relative w-72 h-72 sm:w-80 sm:h-80 rounded-full p-2.5 border border-subtle bg-surface-0 shadow-none flex items-center justify-center">
          
          {/* Subtle SVG Compass / Graticule Tick Ring */}
          <svg
            className="absolute inset-0 w-full h-full pointer-events-none select-none text-muted opacity-70"
            viewBox="0 0 320 320"
          >
            {/* Outer coordinate ring */}
            <circle cx="160" cy="160" r="154" fill="none" stroke="currentColor" strokeWidth="1" strokeDasharray="3 3" />
            <circle cx="160" cy="160" r="148" fill="none" stroke="currentColor" strokeWidth="0.75" />
            
            {/* Degree ticks around perimeter */}
            {Array.from({ length: 36 }).map((_, i) => {
              const deg = i * 10;
              const isMajor = deg % 90 === 0;
              const isMid = deg % 30 === 0;
              const len = isMajor ? 8 : isMid ? 5 : 3;
              const rad = (deg * Math.PI) / 180;
              const x1 = 160 + 148 * Math.cos(rad);
              const y1 = 160 + 148 * Math.sin(rad);
              const x2 = 160 + (148 - len) * Math.cos(rad);
              const y2 = 160 + (148 - len) * Math.sin(rad);
              return (
                <line
                  key={deg}
                  x1={x1}
                  y1={y1}
                  x2={x2}
                  y2={y2}
                  stroke="currentColor"
                  strokeWidth={isMajor ? 1.5 : 0.75}
                />
              );
            })}
            
            {/* Cardinal markings */}
            <text x="160" y="24" textAnchor="middle" fontSize="8" fontFamily="IBM Plex Mono, monospace" fill="currentColor">000°</text>
            <text x="302" y="163" textAnchor="middle" fontSize="8" fontFamily="IBM Plex Mono, monospace" fill="currentColor">090°</text>
            <text x="160" y="306" textAnchor="middle" fontSize="8" fontFamily="IBM Plex Mono, monospace" fill="currentColor">180°</text>
            <text x="18" y="163" textAnchor="middle" fontSize="8" fontFamily="IBM Plex Mono, monospace" fill="currentColor">270°</text>
          </svg>

          {/* Globe Video — preserving original rendering & loop */}
          <div className="relative w-64 h-64 sm:w-72 sm:h-72 rounded-full overflow-hidden border border-strong bg-black">
            <video
              src={globevid}
              autoPlay
              loop
              muted
              playsInline
              className="w-full h-full object-cover rounded-full"
            />
          </div>
        </div>
      </div>

      {/* Hero Subtitle Badge */}
      <div className="mt-8 inline-flex items-center gap-2 px-3 py-1 rounded-[2px] bg-surface-1 border border-subtle text-muted text-[11px] font-mono uppercase tracking-wider">
        <span className="w-1.5 h-1.5 rounded-[1px] bg-state-success shrink-0" />
        <span>ARGO Global Hydrographic Network · Live Survey Instrument</span>
      </div>

      {/* Hero Text */}
      <h1 className="mt-4 text-3xl md:text-5xl font-bold font-heading leading-tight text-primary tracking-tight max-w-3xl">
        Dive into the ocean of data <br />
        with <span className="text-accent">AI</span>. Explore. Visualize. <br />
        Understand.
      </h1>

      {/* Search / Exploration Action Button */}
      <SearchBar />
    </main>
  );
}
