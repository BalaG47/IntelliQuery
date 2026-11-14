import React from "react";
import { FiGlobe, FiDatabase, FiCalendar, FiMapPin } from "react-icons/fi";

/** Normalise an ISO timestamp to YYYY-MM-DD without timezone drift. */
const formatDate = (value) => {
  if (typeof value !== "string" || value.trim() === "") return null;

  // Prefer the leading calendar date so local/UTC offset never shifts the day.
  const iso = value.trim().match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (iso) return `${iso[1]}-${iso[2]}-${iso[3]}`;

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString().slice(0, 10);
};

/** Coerce a coordinate to a finite number, treating null/blank as absent. */
const toNumber = (value) => {
  if (value === null || value === undefined || value === "") return null;
  const num = typeof value === "number" ? value : Number(value);
  return Number.isFinite(num) ? num : null;
};

/** Format a latitude as e.g. 20.68°N / 44.13°S. */
const formatLatitude = (value) => {
  const num = toNumber(value);
  if (num === null) return null;
  return `${Math.abs(num).toFixed(2)}°${num >= 0 ? "N" : "S"}`;
};

/** Format a longitude as e.g. 94.02°E / 39.72°W. */
const formatLongitude = (value) => {
  const num = toNumber(value);
  if (num === null) return null;
  return `${Math.abs(num).toFixed(2)}°${num >= 0 ? "E" : "W"}`;
};

const MISSING = "—";
const NOT_RECORDED = "Not recorded";

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

  const dateStart = formatDate(overview.date_range?.min);
  const dateEnd = formatDate(overview.date_range?.max);
  const hasDateRange = Boolean(dateStart || dateEnd);

  const latMin = formatLatitude(overview.geo_bounds?.min_latitude);
  const latMax = formatLatitude(overview.geo_bounds?.max_latitude);
  const lonMin = formatLongitude(overview.geo_bounds?.min_longitude);
  const lonMax = formatLongitude(overview.geo_bounds?.max_longitude);
  const hasGeoBounds = Boolean(latMin || latMax || lonMin || lonMax);

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
      title: "Date Range",
      lines: hasDateRange
        ? [`${dateStart ?? MISSING}`, `→ ${dateEnd ?? MISSING}`]
        : [NOT_RECORDED],
      icon: <FiCalendar className="text-accent text-lg" />,
      subtext: hasDateRange ? "Earliest → latest observation" : "Observation dates unavailable",
    },
    {
      title: "Geographic Extents",
      lines: hasGeoBounds
        ? [
            `${latMin ?? MISSING} – ${latMax ?? MISSING}`,
            `${lonMin ?? MISSING} – ${lonMax ?? MISSING}`,
          ]
        : [NOT_RECORDED],
      icon: <FiMapPin className="text-accent text-lg" />,
      subtext: hasGeoBounds ? "Bounding box of observations" : "Coordinates unavailable",
    },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
      {cards.map((card, idx) => (
        <div
          key={idx}
          className="bg-surface-0 border border-subtle rounded-[3px] p-4 flex items-center justify-between"
        >
          <div className="min-w-0">
            <p className="text-[11px] uppercase tracking-wider text-muted font-mono font-medium mb-1">
              {card.title}
            </p>
            {card.lines ? (
              <div className="font-heading font-bold tracking-tight tabular-nums leading-tight">
                {card.lines.map((line, lineIdx) => (
                  <p
                    key={lineIdx}
                    className={
                      lineIdx === 0
                        ? "text-base text-primary whitespace-nowrap"
                        : "text-sm text-secondary font-mono whitespace-nowrap"
                    }
                  >
                    {line}
                  </p>
                ))}
              </div>
            ) : (
              <p className="text-2xl font-bold text-primary font-heading tracking-tight">
                {card.value}
              </p>
            )}
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
