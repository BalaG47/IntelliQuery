import React from "react";
import Navbar from "../components/Navbar";
import Hero from "../components/Hero";
import bgImage from "../assets/bg.png"; // make sure your image exists

export default function LandingPage() {
  return (
    <div
      className="min-h-screen flex flex-col bg-cover bg-center"
      style={{ backgroundImage: `url(${bgImage})` }}
    >
      <Navbar />
      <Hero />
    </div>
  );
}
