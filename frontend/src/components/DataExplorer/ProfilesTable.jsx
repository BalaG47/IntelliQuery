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
    if (sortBy !== col) return <span className="opacity-30 ml-1">↕</span>;
    return sortDir === "asc" ? (
      <FiChevronUp className="text-accent ml-1" />
    ) : (
      <FiChevronDown className="text-accent ml-1" />
    );
  };

  const formatDate = (isoStr) => {
    if (!isoStr) return <span className="text-muted">—</span>;
    return isoStr.replace("T", " ");
  };

  return (
    <div className="bg-surface-0 border border-subtle rounded-[3px] overflow-hidden">
      {/* Table Header Info */}
      <div className="p-4 border-b border-subtle flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text-primary font-heading">ARGO Profiles Records</h3>
          <p className="text-xs text-secondary font-mono">
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
        <div className="overflow-x-auto w-full">
          <table className="w-full text-left text-xs min-w-[700px]">
            <thead className="bg-surface-1 border-b border-subtle text-secondary uppercase tracking-wider font-mono text-[11px]">
              <tr>
                <th
                  onClick={() => handleSort("profile_id")}
                  className="px-4 py-3 cursor-pointer hover:text-primary transition-colors select-none font-medium"
                >
                  <div className="flex items-center">
                    <span>Profile ID</span>
                    {renderSortIcon("profile_id")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("wmo_number")}
                  className="px-4 py-3 cursor-pointer hover:text-primary transition-colors select-none font-medium"
                >
                  <div className="flex items-center">
                    <span>WMO Number</span>
                    {renderSortIcon("wmo_number")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("cycle_number")}
                  className="px-4 py-3 cursor-pointer hover:text-primary transition-colors select-none font-medium"
                >
                  <div className="flex items-center">
                    <span>Cycle</span>
                    {renderSortIcon("cycle_number")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("profile_time")}
                  className="px-4 py-3 cursor-pointer hover:text-primary transition-colors select-none font-medium"
                >
                  <div className="flex items-center">
                    <span>Observation Date</span>
                    {renderSortIcon("profile_time")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("latitude")}
                  className="px-4 py-3 cursor-pointer hover:text-primary transition-colors select-none font-medium"
                >
                  <div className="flex items-center">
                    <span>Latitude (°N)</span>
                    {renderSortIcon("latitude")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("longitude")}
                  className="px-4 py-3 cursor-pointer hover:text-primary transition-colors select-none font-medium"
                >
                  <div className="flex items-center">
                    <span>Longitude (°E)</span>
                    {renderSortIcon("longitude")}
                  </div>
                </th>
                <th className="px-4 py-3 text-right font-medium">Levels</th>
                <th className="px-4 py-3 w-16 text-right font-medium">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-subtle text-primary">
              {profiles.map((p) => (
                <tr
                  key={p.profile_id}
                  onClick={() => onSelectProfile && onSelectProfile(p.profile_id)}
                  className={`hover:bg-surface-1/60 transition-colors ${onSelectProfile ? "cursor-pointer" : ""}`}
                >
                  <td className="px-4 py-3 font-mono font-medium text-secondary tabular-nums">
                    #{p.profile_id}
                  </td>
                  <td className="px-4 py-3 font-mono font-medium text-accent tabular-nums">
                    {p.wmo_number}
                  </td>
                  <td className="px-4 py-3 font-mono tabular-nums text-primary">
                    {p.cycle_number}
                  </td>
                  <td className="px-4 py-3 font-mono text-secondary">
                    {formatDate(p.profile_time)}
                  </td>
                  <td className="px-4 py-3 font-mono tabular-nums text-secondary">
                    {p.latitude !== null && p.latitude !== undefined ? (
                      p.latitude.toFixed(4)
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono tabular-nums text-secondary">
                    {p.longitude !== null && p.longitude !== undefined ? (
                      p.longitude.toFixed(4)
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <span className="inline-block px-2 py-0.5 rounded-[2px] bg-surface-1 border border-subtle text-secondary font-mono text-xs tabular-nums">
                      {p.level_count} lvls
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    {onSelectProfile && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelectProfile(p.profile_id);
                        }}
                        className="px-2.5 py-1 text-xs rounded-[2px] border border-subtle bg-surface-1 hover:bg-surface-2 text-primary font-mono transition-colors cursor-pointer"
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
