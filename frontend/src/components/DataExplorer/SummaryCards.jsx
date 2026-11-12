import React from "react";
import { FiGlobe, FiDatabase, FiLayers, FiBriefcase } from "react-icons/fi";

const SummaryCards = ({ overview, loading }) => {
  if (loading && !overview) {
    return (
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        {[1, 2, 3, 4].map((i) => (
          <div
            key={i}
            className="h-24 bg-[#1E2128B3] rounded-xl border border-[#FF1F4B33] animate-pulse p-4"
          />
        ))}
      </div>
    );
  }

  if (!overview) return null;

  // Count active parameters
  const activeParams = ["pressure", "temperature", "salinity", "bgc"].filter(
    (key) => overview.parameters && overview.parameters[key] === true
  ).length;

  const projectsDisplay = overview.projects?.recorded
    ? `${overview.projects.values?.length || 0} Recorded`
    : "Not recorded";

  const cards = [
    {
      title: "Total Floats",
      value: overview.total_floats ?? "--",
      icon: <FiGlobe className="text-blue-400 text-xl" />,
      subtext: "Profiling platforms",
    },
    {
      title: "Total Profiles",
      value: overview.total_profiles ? overview.total_profiles.toLocaleString() : "--",
      icon: <FiDatabase className="text-cyan-400 text-xl" />,
      subtext: "Depth cycles recorded",
    },
    {
      title: "Active Parameters",
      value: activeParams,
      icon: <FiLayers className="text-emerald-400 text-xl" />,
      subtext: "PRES, TEMP, PSAL (BGC: None)",
    },
    {
      title: "Projects",
      value: projectsDisplay,
      icon: <FiBriefcase className="text-purple-400 text-xl" />,
      subtext: overview.projects?.recorded ? "Active institutions" : "NetCDF attribute unavailable",
    },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
      {cards.map((card, idx) => (
        <div
          key={idx}
          className="bg-[#1E2128B3] border border-[#FF1F4B33] rounded-xl p-4 shadow-lg flex items-center justify-between"
        >
          <div>
            <p className="text-xs uppercase tracking-wider text-[#BDC1CAFF] font-medium mb-1">
              {card.title}
            </p>
            <p className="text-2xl font-bold text-white tracking-tight">{card.value}</p>
            <p className="text-xs text-gray-400 mt-1">{card.subtext}</p>
          </div>
          <div className="w-12 h-12 rounded-xl bg-gray-800/80 border border-gray-700/50 flex items-center justify-center">
            {card.icon}
          </div>
        </div>
      ))}
    </div>
  );
};

export default SummaryCards;
