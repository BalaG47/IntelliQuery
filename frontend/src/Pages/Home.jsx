import React, { useState, useEffect } from "react";
import Navbar from "../components/Navbar";
import Sidebar from "../components/Sidebar";
import ChatWindow from "../components/ChatWindow";
import Visualization from "../components/Visualization";

const Home = () => {
  const [showVisualization, setShowVisualization] = useState(false);
  const [fadeInVisualization, setFadeInVisualization] = useState(false);

  // Start fade-in animation when visualization should appear
  useEffect(() => {
    if (showVisualization) {
      const timeout = setTimeout(() => setFadeInVisualization(true), 50);
      return () => clearTimeout(timeout);
    }
  }, [showVisualization]);

  return (
    <div className="bg-app min-h-screen text-primary flex flex-col">
      <Navbar />
      <Sidebar />

      <main className="lg:ml-60 ml-0 pt-16 p-3 sm:p-4 flex-1 flex flex-col min-w-0 h-[calc(100vh-4rem)]">
        <div className="flex-1 flex flex-col lg:flex-row gap-4 min-h-0">
          {/* Left Panel (Chat) */}
          <div className="flex-1 flex flex-col min-w-0 min-h-[460px] lg:min-h-0">
            <ChatWindow onFirstMessage={() => setShowVisualization(true)} />
          </div>

          {/* Right Panel (Visualization Container) */}
          <div className="w-full lg:w-96 xl:w-[460px] shrink-0 bg-surface-0 border border-subtle rounded-[3px] p-4 flex flex-col items-center justify-center overflow-y-auto">
            {/* Placeholder text */}
            {!showVisualization && (
              <div className="text-center p-6">
                <div className="w-8 h-8 rounded-[2px] bg-surface-1 border border-subtle mx-auto mb-2 flex items-center justify-center text-muted">
                  <span className="w-2 h-2 rounded-[1px] bg-accent animate-pulse" />
                </div>
                <p className="text-muted font-mono text-xs uppercase tracking-wider italic">
                  Visualization is starting...
                </p>
                <p className="text-[11px] text-muted font-mono mt-1 opacity-70">
                  Send a query to project oceanographic charts
                </p>
              </div>
            )}

            {/* Visualization component */}
            {showVisualization && (
              <div
                className={`transition-opacity duration-700 w-full ${
                  fadeInVisualization ? "opacity-100" : "opacity-0"
                }`}
              >
                <Visualization />
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  );
};

export default Home;
