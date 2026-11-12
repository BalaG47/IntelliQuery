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
      const timeout = setTimeout(() => setFadeInVisualization(true), 50); // slight delay
      return () => clearTimeout(timeout);
    }
  }, [showVisualization]);

  return (
    <div>
      <Navbar />
      <Sidebar />

      <div className="ml-60 pt-16 p-4 h-screen bg-[#000000FF]  flex gap-4">
        {/* Left Panel (chat) */}
        <div className="max-w-3xl flex flex-1 flex-col">
          <ChatWindow onFirstMessage={() => setShowVisualization(true)} />
        </div>

        {/* Right Panel (visualization container always visible) */}
        <div className="w-80 flex-1 h-full overflow-y-auto bg-gray-800 p-2 rounded-xl shadow-lg flex flex-col items-center justify-center">
          {/* Placeholder text */}
          <p
            className={`text-gray-400 italic text-center transition-opacity duration-500 ${
              showVisualization ? "opacity-0" : "opacity-100"
            }`}
          >
            Visualization is starting...
          </p>

          {/* Visualization component */}
          {showVisualization && (
            <div
              className={`transition-opacity duration-1000 ease-in-out w-full ${
                fadeInVisualization ? "opacity-100" : "opacity-0"
              }`}
            >
              <Visualization />
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default Home;
