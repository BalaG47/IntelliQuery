import React, { useState, useEffect } from "react";
import {
  FiGlobe,
  FiLayers,
  FiMessageSquare,
  FiBarChart2,
  FiChevronDown,
  FiChevronRight,
  FiInfo,
  FiX,
} from "react-icons/fi";
import { NavLink } from "react-router-dom";

const Sidebar = () => {
  const [openMenu, setOpenMenu] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);

  useEffect(() => {
    const handleToggle = () => setMobileOpen((prev) => !prev);
    const handleClose = () => setMobileOpen(false);
    window.addEventListener("toggle-sidebar", handleToggle);
    window.addEventListener("close-sidebar", handleClose);
    return () => {
      window.removeEventListener("toggle-sidebar", handleToggle);
      window.removeEventListener("close-sidebar", handleClose);
    };
  }, []);

  const navLinkClass = ({ isActive }) =>
    `flex items-center gap-3 px-3 py-2 text-xs font-medium rounded-[3px] transition-colors duration-100 ${
      isActive
        ? "text-accent bg-surface-1 font-semibold border-l-2 border-accent"
        : "text-secondary hover:text-primary hover:bg-surface-1/60"
    }`;

  const sidebarContent = (
    <div className="flex flex-col h-full justify-between">
      {/* Top: Brand & Navigation Links */}
      <div>
        {/* Logo / App Name */}
        <div className="mb-6 pt-3 px-1 flex items-start justify-between">
          <div>
            <NavLink
              to="/"
              onClick={() => setMobileOpen(false)}
              className="flex items-baseline gap-1.5 font-bold tracking-tight text-lg text-primary"
            >
              <span>INTELLI</span>
              <span className="text-accent">QUERY</span>
            </NavLink>
            <p className="text-[10px] font-mono uppercase tracking-widest text-muted mt-1">
              ARGO Ocean Console
            </p>
          </div>
          {/* Mobile close button */}
          <button
            type="button"
            onClick={() => setMobileOpen(false)}
            className="lg:hidden p-1 rounded-[3px] text-muted hover:text-primary"
            aria-label="Close Sidebar"
          >
            <FiX className="text-base" />
          </button>
        </div>

        {/* Navigation Rack */}
        <nav className="flex flex-col gap-1">
          {/* Home */}
          <NavLink to="/" end onClick={() => setMobileOpen(false)} className={navLinkClass}>
            <FiGlobe className="text-sm shrink-0" />
            <span>Home</span>
          </NavLink>

          {/* Data Explorer */}
          <NavLink to="/data" onClick={() => setMobileOpen(false)} className={navLinkClass}>
            <FiLayers className="text-sm shrink-0" />
            <span>Data Explorer</span>
          </NavLink>

          {/* Chat */}
          <NavLink to="/chat" onClick={() => setMobileOpen(false)} className={navLinkClass}>
            <FiMessageSquare className="text-sm shrink-0" />
            <span>Chat</span>
          </NavLink>

          {/* Visualizations Accordion */}
          <div>
            <button
              type="button"
              onClick={() => setOpenMenu(!openMenu)}
              className="flex items-center justify-between w-full px-3 py-2 text-xs font-medium rounded-[3px] text-secondary hover:text-primary hover:bg-surface-1/60 transition-colors duration-100 cursor-pointer"
            >
              <span className="flex items-center gap-3">
                <FiBarChart2 className="text-sm shrink-0" />
                <span>Visualizations</span>
              </span>
              {openMenu ? <FiChevronDown className="text-xs" /> : <FiChevronRight className="text-xs" />}
            </button>

            {openMenu && (
              <div className="ml-6 pl-2 border-l border-subtle mt-1 flex flex-col gap-0.5">
                <NavLink
                  to="/visualizations/globe"
                  onClick={() => setMobileOpen(false)}
                  className="px-2 py-1 text-xs text-secondary hover:text-primary hover:bg-surface-1 rounded-[2px]"
                >
                  3D Globe Overlays
                </NavLink>
                <NavLink
                  to="/visualizations/charts"
                  onClick={() => setMobileOpen(false)}
                  className="px-2 py-1 text-xs text-secondary hover:text-primary hover:bg-surface-1 rounded-[2px]"
                >
                  Charts
                </NavLink>
                <NavLink
                  to="/visualizations/flows"
                  onClick={() => setMobileOpen(false)}
                  className="px-2 py-1 text-xs text-secondary hover:text-primary hover:bg-surface-1 rounded-[2px]"
                >
                  Data Flows
                </NavLink>
              </div>
            )}
          </div>

          {/* Insights */}
          <NavLink to="/insights" onClick={() => setMobileOpen(false)} className={navLinkClass}>
            <FiInfo className="text-sm shrink-0" />
            <span>Insights</span>
          </NavLink>
        </nav>
      </div>

      {/* Bottom telemetry footer indicator */}
      <div className="pt-3 border-t border-subtle text-[10px] font-mono text-muted">
        <div className="flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-[#1A633B] dark:bg-[#38D39F] shrink-0 animate-pulse" />
          <span className="font-semibold text-secondary">STATION ONLINE</span>
        </div>
        <p className="mt-1 text-[9px] text-muted">Global ARGO Network</p>
      </div>
    </div>
  );

  return (
    <>
      {/* Desktop Persistent Sidebar */}
      <aside
        className="hidden lg:flex fixed top-14 left-0 h-[calc(100vh-3.5rem)] w-60 bg-surface-0 text-primary border-r border-subtle flex-col p-4 justify-between z-40"
        aria-label="Application Sidebar"
      >
        {sidebarContent}
      </aside>

      {/* Mobile Drawer (visible only when mobileOpen is true below lg) */}
      {mobileOpen && (
        <div className="fixed inset-0 z-50 lg:hidden flex">
          {/* Backdrop */}
          <div
            className="fixed inset-0 bg-black/50 transition-opacity"
            onClick={() => setMobileOpen(false)}
            aria-hidden="true"
          />
          {/* Drawer container */}
          <aside
            className="relative w-64 max-w-[80vw] h-full bg-surface-0 text-primary border-r border-subtle p-4 shadow-xl flex flex-col justify-between z-10"
            aria-label="Mobile Navigation Sidebar"
          >
            {sidebarContent}
          </aside>
        </div>
      )}
    </>
  );
};

export default Sidebar;
