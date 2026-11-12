import React from "react";
import { useTheme } from "../context/ThemeContext";
import { FiSun, FiMoon } from "react-icons/fi";

const ThemeToggle = ({ className = "" }) => {
  const { theme, isDark, toggleTheme } = useTheme();

  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={`Switch to ${isDark ? "light" : "dark"} mode (currently ${theme})`}
      title={`Switch to ${isDark ? "light" : "dark"} mode`}
      className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-mono font-medium rounded-[3px] border transition-colors duration-100 cursor-pointer ${
        isDark
          ? "bg-[#17232E] border-[#223344] text-[#E5EDF5] hover:bg-[#1F2E3D] hover:border-[#60809E] focus:outline-none focus:ring-1 focus:ring-[#48C9A6]"
          : "bg-[#EBE5DA] border-[#DDD7CB] text-[#141C24] hover:bg-[#DFD7C8] hover:border-[#786C5A] focus:outline-none focus:ring-1 focus:ring-[#0E5E6F]"
      } ${className}`}
    >
      {isDark ? (
        <>
          <FiSun className="text-amber-400 text-sm shrink-0" aria-hidden="true" />
          <span className="hidden sm:inline uppercase tracking-wider text-[11px]">Light</span>
        </>
      ) : (
        <>
          <FiMoon className="text-[#0E5E6F] text-sm shrink-0" aria-hidden="true" />
          <span className="hidden sm:inline uppercase tracking-wider text-[11px]">Dark</span>
        </>
      )}
    </button>
  );
};

export default ThemeToggle;
