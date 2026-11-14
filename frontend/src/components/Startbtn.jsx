import React from "react";
import { FiSearch } from "react-icons/fi";
import { Link } from "react-router-dom";

export default function StartBtn() {
  return (
    <div className="relative w-full max-w-lg mt-8 flex justify-center">
      <Link
        to="/chat"
        className="flex items-center justify-center gap-2 px-6 py-3 rounded-[3px] bg-accent text-accent-contrast font-mono text-xs uppercase tracking-wider font-semibold hover:opacity-90 transition-opacity cursor-pointer border border-accent shadow-none"
      >
        <FiSearch className="text-sm" />
        <span>Start Your Exploration</span>
      </Link>
    </div>
  );
}
