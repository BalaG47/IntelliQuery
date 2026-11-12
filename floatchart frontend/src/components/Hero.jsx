import React from "react";
import SearchBar from "./Startbtn";
import globevid from "../assets/abcd.mp4";

export default function Hero() {
  return (
    <main className="flex flex-col items-center justify-center flex-1 px-6 text-center text-white">
      
      {/* Animated Globe */}
      <div className="relative w-72 h-72 ">
         <video src={globevid} autoPlay loop muted className="w-full h-full object-cover rounded-[100%]" />
      </div>

      {/* Hero Text */}
      <h1 className="mt-8 text-3xl md:text-5xl font-bold leading-tight">
        Dive into the ocean of data <br />
        with <span className="text-blue-400">AI</span>. Explore. Visualize. <br />
        Understand.
      </h1>

      {/* Search Bar */}
      <SearchBar />
    </main>
  );
}
