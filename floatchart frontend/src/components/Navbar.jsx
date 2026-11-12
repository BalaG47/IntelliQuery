import React, { useState } from "react";
import { NavLink } from "react-router-dom";
import {
  FiGlobe,
  FiLayers,
  FiMessageSquare,
  FiBarChart2,
  FiChevronDown,
  FiChevronRight,
  FiInfo,
} from "react-icons/fi";
import ThemeToggle from "./ThemeToggle";

export default function Navbar() {
  const [openMenu, setOpenMenu] = useState(false);

  const navLinkClass = ({ isActive }) =>
    `flex items-center gap-2 px-3 py-1.5 text-xs font-medium rounded-[3px] transition-colors duration-100 ${
      isActive
        ? "text-[#0E5E6F] dark:text-[#48C9A6] bg-[#EBE5DA] dark:bg-[#17232E] font-semibold border-b sm:border-b-2 border-[#0E5E6F] dark:border-[#48C9A6]"
        : "text-[#475569] dark:text-[#9AB0C4] hover:text-[#141C24] dark:hover:text-[#E5EDF5] hover:bg-[#EBE5DA]/60 dark:hover:bg-[#17232E]/60"
    }`;

  return (
    <header className="fixed top-0 left-0 w-full z-50 bg-[#FAF8F5] dark:bg-[#111A22] text-[#141C24] dark:text-[#E5EDF5] border-b border-[#DDD7CB] dark:border-[#223344] flex items-center justify-between h-14 px-4 sm:px-6">
      {/* Brand logo & instrument badge */}
      <div className="flex items-center gap-3">
        <NavLink to="/" className="flex items-baseline gap-1.5 font-bold tracking-tight text-lg text-[#141C24] dark:text-[#E5EDF5]">
          <span>FLOAT</span>
          <span className="text-[#0E5E6F] dark:text-[#48C9A6]">CHAT</span>
          <span className="hidden md:inline ml-2 text-[10px] font-mono font-normal uppercase tracking-widest text-[#4F5E6E] dark:text-[#8BA2B6] border-l border-[#DDD7CB] dark:border-[#223344] pl-2">
            ARGO Survey
          </span>
        </NavLink>
      </div>

      {/* Centered navigation links */}
      <nav className="hidden md:flex items-center gap-2">
        <NavLink to="/" end className={navLinkClass}>
          <FiGlobe className="text-sm shrink-0" />
          <span>Home</span>
        </NavLink>

        <NavLink to="/data" className={navLinkClass}>
          <FiLayers className="text-sm shrink-0" />
          <span>Data Explorer</span>
        </NavLink>

        <NavLink to="/chat" className={navLinkClass}>
          <FiMessageSquare className="text-sm shrink-0" />
          <span>Chat</span>
        </NavLink>

        {/* Visualizations Dropdown */}
        <div className="relative">
          <button
            type="button"
            onClick={() => setOpenMenu(!openMenu)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-[3px] text-[#475569] dark:text-[#9AB0C4] hover:text-[#141C24] dark:hover:text-[#E5EDF5] hover:bg-[#EBE5DA]/60 dark:hover:bg-[#17232E]/60 transition-colors duration-100 cursor-pointer"
          >
            <FiBarChart2 className="text-sm shrink-0" />
            <span>Visualizations</span>
            {openMenu ? <FiChevronDown className="text-xs ml-0.5" /> : <FiChevronRight className="text-xs ml-0.5" />}
          </button>

          {openMenu && (
            <div
              className="absolute left-0 top-full mt-1.5 w-48 bg-[#FAF8F5] dark:bg-[#111A22] border border-[#DDD7CB] dark:border-[#223344] shadow-md rounded-[3px] py-1.5 z-50 text-xs font-medium"
              onMouseLeave={() => setOpenMenu(false)}
            >
              <NavLink
                to="/visualizations/globe"
                onClick={() => setOpenMenu(false)}
                className="block px-3 py-1.5 text-[#475569] dark:text-[#9AB0C4] hover:text-[#141C24] dark:hover:text-[#E5EDF5] hover:bg-[#EBE5DA] dark:hover:bg-[#17232E]"
              >
                3D Globe Overlays
              </NavLink>
              <NavLink
                to="/visualizations/charts"
                onClick={() => setOpenMenu(false)}
                className="block px-3 py-1.5 text-[#475569] dark:text-[#9AB0C4] hover:text-[#141C24] dark:hover:text-[#E5EDF5] hover:bg-[#EBE5DA] dark:hover:bg-[#17232E]"
              >
                Charts
              </NavLink>
              <NavLink
                to="/visualizations/flows"
                onClick={() => setOpenMenu(false)}
                className="block px-3 py-1.5 text-[#475569] dark:text-[#9AB0C4] hover:text-[#141C24] dark:hover:text-[#E5EDF5] hover:bg-[#EBE5DA] dark:hover:bg-[#17232E]"
              >
                Data Flows
              </NavLink>
            </div>
          )}
        </div>

        <NavLink to="/insights" className={navLinkClass}>
          <FiInfo className="text-sm shrink-0" />
          <span>Insights</span>
        </NavLink>
      </nav>

      {/* Right controls: Theme toggle */}
      <div className="flex items-center gap-3">
        <ThemeToggle />
      </div>
    </header>
  );
}
