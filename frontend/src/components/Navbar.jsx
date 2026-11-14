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
  FiMenu,
} from "react-icons/fi";
import ThemeToggle from "./ThemeToggle";

export default function Navbar() {
  const [openMenu, setOpenMenu] = useState(false);

  const navLinkClass = ({ isActive }) =>
    `flex items-center gap-2 px-3 py-1.5 text-xs font-medium rounded-[3px] transition-colors duration-100 ${
      isActive
        ? "text-accent bg-surface-1 font-semibold border-b sm:border-b-2 border-accent"
        : "text-secondary hover:text-primary hover:bg-surface-1/60"
    }`;

  return (
    <header className="fixed top-0 left-0 w-full z-50 bg-surface-0 text-primary border-b border-subtle flex items-center justify-between h-14 px-4 sm:px-6">
      {/* Brand logo & instrument badge & mobile menu toggle */}
      <div className="flex items-center gap-2.5 sm:gap-3">
        <button
          type="button"
          onClick={() => window.dispatchEvent(new CustomEvent("toggle-sidebar"))}
          className="lg:hidden p-1.5 rounded-[3px] border border-subtle text-secondary hover:text-primary hover:bg-surface-1 transition-colors"
          aria-label="Toggle Navigation Sidebar"
        >
          <FiMenu className="text-base" />
        </button>

        <NavLink to="/" className="flex items-baseline gap-1.5 font-bold tracking-tight text-lg text-primary">
          <span>INTELLI</span>
          <span className="text-accent">QUERY</span>
          <span className="hidden md:inline ml-2 text-[10px] font-mono font-normal uppercase tracking-widest text-muted border-l border-subtle pl-2">
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
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-[3px] text-secondary hover:text-primary hover:bg-surface-1/60 transition-colors duration-100 cursor-pointer"
          >
            <FiBarChart2 className="text-sm shrink-0" />
            <span>Visualizations</span>
            {openMenu ? <FiChevronDown className="text-xs ml-0.5" /> : <FiChevronRight className="text-xs ml-0.5" />}
          </button>

          {openMenu && (
            <div
              className="absolute left-0 top-full mt-1.5 w-48 bg-surface-0 border border-subtle shadow-md rounded-[3px] py-1.5 z-50 text-xs font-medium"
              onMouseLeave={() => setOpenMenu(false)}
            >
              <NavLink
                to="/visualizations/globe"
                onClick={() => setOpenMenu(false)}
                className="block px-3 py-1.5 text-secondary hover:text-primary hover:bg-surface-1"
              >
                3D Globe Overlays
              </NavLink>
              <NavLink
                to="/visualizations/charts"
                onClick={() => setOpenMenu(false)}
                className="block px-3 py-1.5 text-secondary hover:text-primary hover:bg-surface-1"
              >
                Charts
              </NavLink>
              <NavLink
                to="/visualizations/flows"
                onClick={() => setOpenMenu(false)}
                className="block px-3 py-1.5 text-secondary hover:text-primary hover:bg-surface-1"
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
