import React, { useState } from "react";
import {
  FiGlobe,
  FiLayers,
  FiMessageSquare,
  FiBarChart2,
  FiChevronDown,
  FiChevronRight,
  FiInfo,
} from "react-icons/fi";
import { NavLink } from "react-router-dom";

const Sidebar = () => {
  const [openMenu, setOpenMenu] = useState(false);

  const navLinkClass = ({ isActive }) =>
    `flex items-center gap-3 px-3 py-2 text-xs font-medium rounded-[3px] transition-colors duration-100 ${
      isActive
        ? "text-[#0E5E6F] dark:text-[#48C9A6] bg-[#EBE5DA] dark:bg-[#17232E] font-semibold border-l-2 border-[#0E5E6F] dark:border-[#48C9A6]"
        : "text-[#475569] dark:text-[#9AB0C4] hover:text-[#141C24] dark:hover:text-[#E5EDF5] hover:bg-[#EBE5DA]/60 dark:hover:bg-[#17232E]/60"
    }`;

  return (
    <aside
      className="fixed top-0 left-0 h-full w-60 bg-[#FAF8F5] dark:bg-[#111A22] text-[#141C24] dark:text-[#E5EDF5] border-r border-[#DDD7CB] dark:border-[#223344] flex flex-col p-4 justify-between z-40"
      aria-label="Application Sidebar"
    >
      {/* Top: Brand & Navigation Links */}
      <div>
        {/* Logo / App Name */}
        <div className="mb-6 pt-2 px-1">
          <NavLink
            to="/"
            className="flex items-baseline gap-1.5 font-bold tracking-tight text-xl text-[#141C24] dark:text-[#E5EDF5]"
          >
            <span>INTELLI</span>
            <span className="text-[#0E5E6F] dark:text-[#48C9A6]">QUERY</span>
          </NavLink>
          <p className="text-[10px] font-mono uppercase tracking-widest text-[#4F5E6E] dark:text-[#8BA2B6] mt-0.5">
            ARGO Ocean Console — IntelliQuery
          </p>
        </div>

        {/* Navigation Rack */}
        <nav className="flex flex-col gap-1">
          {/* Home */}
          <NavLink to="/" end className={navLinkClass}>
            <FiGlobe className="text-sm shrink-0" />
            <span>Home</span>
          </NavLink>

          {/* Data Explorer */}
          <NavLink to="/data" className={navLinkClass}>
            <FiLayers className="text-sm shrink-0" />
            <span>Data Explorer</span>
          </NavLink>

          {/* Chat */}
          <NavLink to="/chat" className={navLinkClass}>
            <FiMessageSquare className="text-sm shrink-0" />
            <span>Chat</span>
          </NavLink>

          {/* Visualizations Accordion */}
          <div>
            <button
              type="button"
              onClick={() => setOpenMenu(!openMenu)}
              className="flex items-center justify-between w-full px-3 py-2 text-xs font-medium rounded-[3px] text-[#475569] dark:text-[#9AB0C4] hover:text-[#141C24] dark:hover:text-[#E5EDF5] hover:bg-[#EBE5DA]/60 dark:hover:bg-[#17232E]/60 transition-colors duration-100 cursor-pointer"
            >
              <span className="flex items-center gap-3">
                <FiBarChart2 className="text-sm shrink-0" />
                <span>Visualizations</span>
              </span>
              {openMenu ? <FiChevronDown className="text-xs" /> : <FiChevronRight className="text-xs" />}
            </button>

            {openMenu && (
              <div className="ml-6 pl-2 border-l border-[#DDD7CB] dark:border-[#223344] mt-1 flex flex-col gap-0.5">
                <NavLink
                  to="/visualizations/globe"
                  className="px-2 py-1 text-xs text-[#475569] dark:text-[#9AB0C4] hover:text-[#141C24] dark:hover:text-[#E5EDF5] hover:bg-[#EBE5DA] dark:hover:bg-[#17232E] rounded-[2px]"
                >
                  3D Globe Overlays
                </NavLink>
                <NavLink
                  to="/visualizations/charts"
                  className="px-2 py-1 text-xs text-[#475569] dark:text-[#9AB0C4] hover:text-[#141C24] dark:hover:text-[#E5EDF5] hover:bg-[#EBE5DA] dark:hover:bg-[#17232E] rounded-[2px]"
                >
                  Charts
                </NavLink>
                <NavLink
                  to="/visualizations/flows"
                  className="px-2 py-1 text-xs text-[#475569] dark:text-[#9AB0C4] hover:text-[#141C24] dark:hover:text-[#E5EDF5] hover:bg-[#EBE5DA] dark:hover:bg-[#17232E] rounded-[2px]"
                >
                  Data Flows
                </NavLink>
              </div>
            )}
          </div>

          {/* Insights */}
          <NavLink to="/insights" className={navLinkClass}>
            <FiInfo className="text-sm shrink-0" />
            <span>Insights</span>
          </NavLink>
        </nav>
      </div>

      {/* Bottom telemetry footer indicator */}
      <div className="pt-3 border-t border-[#DDD7CB] dark:border-[#223344] text-[10px] font-mono text-[#4F5E6E] dark:text-[#8BA2B6]">
        <div className="flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-[#1A633B] dark:bg-[#38D39F] shrink-0" />
          <span>STATION ONLINE</span>
        </div>
        <p className="mt-1 text-[9px] text-[#7A889B] dark:text-[#5E6E80]">Global ARGO Network</p>
      </div>
    </aside>
  );
};

export default Sidebar;
