import React from "react";
import { FiChevronUp, FiChevronDown } from "react-icons/fi";
import Pagination from "./Pagination";
import Loading from "./Loading";
import EmptyState from "./EmptyState";
import ErrorState from "./ErrorState";

const ProfilesTable = ({
  profiles,
  pagination,
  loading,
  error,
  sortBy,
  sortDir,
  onSort,
  onPageChange,
  onClearFilters,
  onRetry,
  onSelectProfile,
}) => {
  const handleSort = (col) => {
    if (sortBy === col) {
      onSort(col, sortDir === "asc" ? "desc" : "asc");
    } else {
      onSort(col, "asc");
    }
  };

  const renderSortIcon = (col) => {
    if (sortBy !== col) return <span className="opacity-30">↕</span>;
    return sortDir === "asc" ? <FiChevronUp className="text-blue-400" /> : <FiChevronDown className="text-blue-400" />;
  };

  const formatDate = (isoStr) => {
    if (!isoStr) return "--";
    return isoStr.replace("T", " ");
  };

  return (
    <div className="bg-[#1E2128B3] border border-[#FF1F4B33] rounded-xl shadow-lg overflow-hidden">
      {/* Table Header Info */}
      <div className="p-4 border-b border-gray-800 flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text-white">ARGO Profiles Records</h3>
          <p className="text-xs text-[#BDC1CAFF]">
            Index of ocean vertical profiling cycles and spatial observation coordinates.
          </p>
        </div>
      </div>

      {/* Main Content States */}
      {loading ? (
        <Loading message="Loading profiles data..." />
      ) : error ? (
        <div className="p-4">
          <ErrorState message={error} onRetry={onRetry} />
        </div>
      ) : profiles.length === 0 ? (
        <EmptyState
          title="No profiles found"
          message="No ocean profiles match the current filter parameters."
          onClear={onClearFilters}
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="bg-gray-900/80 border-b border-gray-800 text-[#BDC1CAFF] uppercase tracking-wider font-medium">
              <tr>
                <th
                  onClick={() => handleSort("profile_id")}
                  className="px-4 py-3 cursor-pointer hover:text-white transition select-none"
                >
                  <div className="flex items-center gap-1.5">
                    <span>Profile ID</span>
                    {renderSortIcon("profile_id")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("wmo_number")}
                  className="px-4 py-3 cursor-pointer hover:text-white transition select-none"
                >
                  <div className="flex items-center gap-1.5">
                    <span>WMO Number</span>
                    {renderSortIcon("wmo_number")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("cycle_number")}
                  className="px-4 py-3 cursor-pointer hover:text-white transition select-none"
                >
                  <div className="flex items-center gap-1.5">
                    <span>Cycle</span>
                    {renderSortIcon("cycle_number")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("profile_time")}
                  className="px-4 py-3 cursor-pointer hover:text-white transition select-none"
                >
                  <div className="flex items-center gap-1.5">
                    <span>Observation Date</span>
                    {renderSortIcon("profile_time")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("latitude")}
                  className="px-4 py-3 cursor-pointer hover:text-white transition select-none"
                >
                  <div className="flex items-center gap-1.5">
                    <span>Latitude (°N)</span>
                    {renderSortIcon("latitude")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("longitude")}
                  className="px-4 py-3 cursor-pointer hover:text-white transition select-none"
                >
                  <div className="flex items-center gap-1.5">
                    <span>Longitude (°E)</span>
                    {renderSortIcon("longitude")}
                  </div>
                </th>
                <th className="px-4 py-3 text-right">Levels</th>
                <th className="px-4 py-3 w-14"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800 text-gray-200">
              {profiles.map((p) => (
                <tr
                  key={p.profile_id}
                  onClick={() => onSelectProfile && onSelectProfile(p.profile_id)}
                  className={`hover:bg-gray-800/50 transition ${onSelectProfile ? "cursor-pointer" : ""}`}
                >
                  <td className="px-4 py-3 font-mono font-medium text-gray-300">
                    #{p.profile_id}
                  </td>
                  <td className="px-4 py-3 font-mono font-semibold text-blue-300">
                    {p.wmo_number}
                  </td>
                  <td className="px-4 py-3 font-mono">
                    {p.cycle_number}
                  </td>
                  <td className="px-4 py-3 font-mono text-gray-400">
                    {formatDate(p.profile_time)}
                  </td>
                  <td className="px-4 py-3 font-mono">
                    {p.latitude !== null ? p.latitude.toFixed(4) : "--"}
                  </td>
                  <td className="px-4 py-3 font-mono">
                    {p.longitude !== null ? p.longitude.toFixed(4) : "--"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <span className="inline-block px-2 py-0.5 rounded-full bg-cyan-950/80 border border-cyan-800/50 text-cyan-300 font-mono text-xs">
                      {p.level_count} lvls
                    </span>
                  </td>
                  <td className="px-3 py-3 text-right">
                    {onSelectProfile && (
                      <button
                        onClick={(e) => { e.stopPropagation(); onSelectProfile(p.profile_id); }}
                        className="px-2 py-0.5 text-xs rounded border border-blue-700/50 text-blue-400 hover:bg-blue-900/40 transition"
                      >
                        View
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination Footer */}
      {!loading && !error && pagination && (
        <Pagination
          page={pagination.page}
          totalPages={pagination.total_pages}
          total={pagination.total}
          limit={pagination.limit}
          onPageChange={onPageChange}
          loading={loading}
        />
      )}
    </div>
  );
};

export default ProfilesTable;
