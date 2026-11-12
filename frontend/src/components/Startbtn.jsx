import React from "react";
import { FiSearch } from "react-icons/fi";
import { Link } from "react-router-dom";

export default function StartBtn() {
  return (
    <div className="relative w-full max-w-lg mt-8 flex justify-center">
      <Link
        to="/chat"
        className="flex items-center justify-center gap-2 px-6 py-3 
                   rounded-full bg-blue-600 text-white font-semibold 
                   hover:bg-blue-700 transition duration-300 shadow-md"
      >
        <FiSearch className="text-lg" />
        Start Your Exploration
      </Link>
    </div>
  );
}
