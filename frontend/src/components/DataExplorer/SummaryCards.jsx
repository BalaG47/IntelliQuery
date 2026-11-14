import React from "react";
import { FiGlobe, FiDatabase, FiLayers, FiBriefcase } from "react-icons/fi";

const SummaryCards = ({ overview, loading }) => {
  if (loading && !overview) {
    return (
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
        {[1, 2, 3, 4].map((i) => (
          <div
            key={i}
            className="h-24 bg-surface-1 rounded-[3px] border border-subtle animate-pulse p-4"
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

  // Filter out 'N/A', 'Unknown', or empty project names to only count real values
  const validProjects = (overview.projects?.values || []).filter(
    (v) => v && typeof v === "string" && v !== "N/A" && v !== "Unknown" && v.trim() !== ""
  );
  const projectsCount = validProjects.length;
  const projectsDisplay = projectsCount > 0 ? `${projectsCount} Recorded` : "Not recorded";

  const cards = [
    {
      title: "Total Floats",
      value: overview.total_floats ?? "--",
      icon: <FiGlobe className="text-accent text-lg" />,
      subtext: "Profiling platforms",
    },
    {
      title: "Total Profiles",
      value: overview.total_profiles ? overview.total_profiles.toLocaleString() : "--",
      icon: <FiDatabase className="text-accent text-lg" />,
      subtext: "Depth cycles recorded",
    },
    {
      title: "Active Parameters",
      value: activeParams,
      icon: <FiLayers className="text-accent text-lg" />,
      subtext: "PRES, TEMP, PSAL (BGC: None)",
    },
    {
      title: "Projects",
      value: projectsDisplay,
      icon: <FiBriefcase className="text-accent text-lg" />,
      subtext: projectsCount > 0 ? "Project names recorded" : "NetCDF metadata unavailable",
    },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
      {cards.map((card, idx) => (
        <div
          key={idx}
          className="bg-surface-0 border border-subtle rounded-[3px] p-4 flex items-center justify-between"
        >
          <div>
            <p className="text-[11px] uppercase tracking-wider text-muted font-mono font-medium mb-1">
              {card.title}
            </p>
            <p className="text-2xl font-bold text-primary font-heading tracking-tight">{card.value}</p>
            <p className="text-xs text-secondary mt-1">{card.subtext}</p>
          </div>
          <div className="w-10 h-10 rounded-[3px] bg-surface-1 border border-subtle flex items-center justify-center shrink-0">
            {card.icon}
          </div>
        </div>
      ))}
    </div>
  );
};

export default SummaryCards;
