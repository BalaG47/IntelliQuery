import React, { useState, useEffect } from "react";
import { FiFilter, FiX, FiCheck, FiRotateCcw, FiAlertCircle } from "react-icons/fi";

const FilterPanel = ({ filters, onApply, onClear, resultCount, activeTab }) => {
  const [localFilters, setLocalFilters] = useState(filters);
  const [validationError, setValidationError] = useState(null);

  // Sync when external filters change (e.g., chip removed or WMO set from Floats tab)
  useEffect(() => {
    setLocalFilters(filters);
  }, [filters]);

  const handleChange = (field, value) => {
    setLocalFilters((prev) => ({
      ...prev,
      [field]: value,
    }));
  };

  const validate = () => {
    // Latitude check
    if (localFilters.min_lat !== "" && localFilters.max_lat !== "") {
      const minLat = parseFloat(localFilters.min_lat);
      const maxLat = parseFloat(localFilters.max_lat);
      if (!isNaN(minLat) && !isNaN(maxLat) && minLat > maxLat) {
        return "Min Latitude cannot be greater than Max Latitude.";
      }
    }
    // Longitude check
    if (localFilters.min_lon !== "" && localFilters.max_lon !== "") {
      const minLon = parseFloat(localFilters.min_lon);
      const maxLon = parseFloat(localFilters.max_lon);
      if (!isNaN(minLon) && !isNaN(maxLon) && minLon > maxLon) {
        return "Min Longitude cannot be greater than Max Longitude.";
      }
    }
    // Date check
    if (localFilters.start_date && localFilters.end_date) {
      if (new Date(localFilters.start_date) > new Date(localFilters.end_date)) {
        return "Start Date cannot be later than End Date.";
      }
    }
    return null;
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    const err = validate();
    if (err) {
      setValidationError(err);
      return;
    }
    setValidationError(null);
    onApply(localFilters);
  };

  const handleClear = () => {
    setValidationError(null);
    onClear();
  };

  const removeChip = (field) => {
    const updated = { ...filters, [field]: "" };
    onApply(updated);
  };

  // Build active filter chips
  const activeChips = [];
  if (filters.wmo_number) activeChips.push({ field: "wmo_number", label: `WMO: ${filters.wmo_number}` });
  if (filters.profile_id) activeChips.push({ field: "profile_id", label: `Profile ID: ${filters.profile_id}` });
  if (filters.cycle_number) activeChips.push({ field: "cycle_number", label: `Cycle: ${filters.cycle_number}` });
  if (filters.start_date) activeChips.push({ field: "start_date", label: `From: ${filters.start_date}` });
  if (filters.end_date) activeChips.push({ field: "end_date", label: `To: ${filters.end_date}` });
  if (filters.min_lat || filters.max_lat) {
    activeChips.push({
      field: "lat_range",
      clearFields: ["min_lat", "max_lat"],
      label: `Lat: [${filters.min_lat || "-90"}°, ${filters.max_lat || "90"}°]`,
    });
  }
  if (filters.min_lon || filters.max_lon) {
    activeChips.push({
      field: "lon_range",
      clearFields: ["min_lon", "max_lon"],
      label: `Lon: [${filters.min_lon || "-180"}°, ${filters.max_lon || "180"}°]`,
    });
  }

  return (
    <div className="bg-[#1E2128B3] border border-[#FF1F4B33] rounded-xl p-4 mb-6 shadow-lg">
      <div className="flex items-center justify-between mb-3 border-b border-gray-800 pb-2">
        <div className="flex items-center gap-2 text-white font-medium text-sm">
          <FiFilter className="text-blue-400" />
          <span>Filters & Constraints</span>
          {resultCount !== undefined && (
            <span className="ml-2 text-xs font-semibold px-2.5 py-0.5 rounded-full bg-blue-900/60 text-blue-300 border border-blue-700/50">
              {resultCount.toLocaleString()} {resultCount === 1 ? "result" : "results"}
            </span>
          )}
        </div>
        {activeChips.length > 0 && (
          <button
            type="button"
            onClick={handleClear}
            className="flex items-center gap-1 text-xs text-gray-400 hover:text-red-400 transition"
          >
            <FiRotateCcw className="text-xs" />
            <span>Reset All</span>
          </button>
        )}
      </div>

      {/* Validation Error Alert */}
      {validationError && (
        <div className="flex items-center gap-2 p-3 mb-4 rounded-lg bg-red-900/30 border border-red-500/50 text-red-200 text-xs">
          <FiAlertCircle className="text-red-400 text-base shrink-0" />
          <span>{validationError}</span>
        </div>
      )}

      {/* Active Filter Chips */}
      {activeChips.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 mb-4">
          <span className="text-xs text-gray-400 font-medium">Active:</span>
          {activeChips.map((chip) => (
            <span
              key={chip.field}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-blue-950/80 border border-blue-500/40 text-blue-200 text-xs font-medium"
            >
              <span>{chip.label}</span>
              <button
                type="button"
                onClick={() => {
                  if (chip.clearFields) {
                    const updated = { ...filters };
                    chip.clearFields.forEach((f) => (updated[f] = ""));
                    onApply(updated);
                  } else {
                    removeChip(chip.field);
                  }
                }}
                className="hover:text-white transition"
                title="Remove filter"
              >
                <FiX className="text-xs" />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Filter Inputs Form */}
      <form onSubmit={handleSubmit}>
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3 text-xs">
          {/* WMO Number */}
          <div>
            <label className="block text-[#BDC1CAFF] mb-1 font-medium">Float WMO Number</label>
            <input
              type="text"
              placeholder="e.g. 1900121"
              value={localFilters.wmo_number || ""}
              onChange={(e) => handleChange("wmo_number", e.target.value)}
              className="w-full px-3 py-1.5 bg-gray-900/90 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500"
            />
          </div>

          {/* Profile ID */}
          <div>
            <label className="block text-[#BDC1CAFF] mb-1 font-medium">Profile ID</label>
            <input
              type="number"
              placeholder="e.g. 50"
              value={localFilters.profile_id || ""}
              onChange={(e) => handleChange("profile_id", e.target.value)}
              className="w-full px-3 py-1.5 bg-gray-900/90 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500"
            />
          </div>

          {/* Cycle Number */}
          <div>
            <label className="block text-[#BDC1CAFF] mb-1 font-medium">Cycle Number</label>
            <input
              type="number"
              placeholder="e.g. 45"
              value={localFilters.cycle_number || ""}
              onChange={(e) => handleChange("cycle_number", e.target.value)}
              className="w-full px-3 py-1.5 bg-gray-900/90 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500"
            />
          </div>

          {/* Date Range */}
          <div>
            <label className="block text-[#BDC1CAFF] mb-1 font-medium">Date Range (From - To)</label>
            <div className="flex items-center gap-1">
              <input
                type="date"
                value={localFilters.start_date || ""}
                onChange={(e) => handleChange("start_date", e.target.value)}
                className="w-1/2 px-2 py-1.5 bg-gray-900/90 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500 text-xs"
              />
              <span className="text-gray-500">-</span>
              <input
                type="date"
                value={localFilters.end_date || ""}
                onChange={(e) => handleChange("end_date", e.target.value)}
                className="w-1/2 px-2 py-1.5 bg-gray-900/90 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500 text-xs"
              />
            </div>
          </div>

          {/* Latitude Range */}
          <div>
            <label className="block text-[#BDC1CAFF] mb-1 font-medium">Latitude Range (°N)</label>
            <div className="flex items-center gap-1">
              <input
                type="number"
                step="any"
                placeholder="Min Lat"
                value={localFilters.min_lat || ""}
                onChange={(e) => handleChange("min_lat", e.target.value)}
                className="w-1/2 px-2 py-1.5 bg-gray-900/90 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500"
              />
              <span className="text-gray-500">-</span>
              <input
                type="number"
                step="any"
                placeholder="Max Lat"
                value={localFilters.max_lat || ""}
                onChange={(e) => handleChange("max_lat", e.target.value)}
                className="w-1/2 px-2 py-1.5 bg-gray-900/90 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>

          {/* Longitude Range */}
          <div>
            <label className="block text-[#BDC1CAFF] mb-1 font-medium">Longitude Range (°E)</label>
            <div className="flex items-center gap-1">
              <input
                type="number"
                step="any"
                placeholder="Min Lon"
                value={localFilters.min_lon || ""}
                onChange={(e) => handleChange("min_lon", e.target.value)}
                className="w-1/2 px-2 py-1.5 bg-gray-900/90 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500"
              />
              <span className="text-gray-500">-</span>
              <input
                type="number"
                step="any"
                placeholder="Max Lon"
                value={localFilters.max_lon || ""}
                onChange={(e) => handleChange("max_lon", e.target.value)}
                className="w-1/2 px-2 py-1.5 bg-gray-900/90 border border-gray-700 rounded-lg text-white focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>

          {/* Actions */}
          <div className="sm:col-span-2 md:col-span-3 lg:col-span-2 flex items-end gap-2 pt-2">
            <button
              type="submit"
              className="flex-1 flex items-center justify-center gap-1.5 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white font-medium rounded-lg transition"
            >
              <FiCheck className="text-sm" />
              <span>Apply Filters</span>
            </button>
            <button
              type="button"
              onClick={handleClear}
              className="px-4 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 font-medium rounded-lg transition"
            >
              Clear
            </button>
          </div>
        </div>
      </form>
    </div>
  );
};

export default FilterPanel;
