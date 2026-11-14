import React from "react";
import { FiChevronUp, FiChevronDown, FiList, FiInfo } from "react-icons/fi";
import Pagination from "./Pagination";
import Loading from "./Loading";
import EmptyState from "./EmptyState";
import ErrorState from "./ErrorState";

const FloatsTable = ({
  floats,
  pagination,
  loading,
  error,
  sortBy,
  sortDir,
  onSort,
  onPageChange,
  onSelectWmo,
  projectsRecorded,
  onRetry,
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
      {/* Table Header Note */}
      <div className="p-4 border-b border-subtle flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div>
          <h3 className="text-base font-semibold text-primary font-heading">ARGO Floats Directory</h3>
          <p className="text-xs text-secondary font-mono">
            Overview of registered ocean profiling floats and total cycles.
          </p>
        </div>
        {/* Only show banner when overview loaded and explicitly reports recorded === false */}
        {projectsRecorded === false && (
          <div className="flex items-center gap-1.5 text-xs text-state-warning bg-surface-1 border border-state-warning/40 px-2.5 py-1 rounded-[2px] font-mono">
            <FiInfo className="text-state-warning shrink-0" />
            <span>Project and platform are not recorded in the current database.</span>
          </div>
        )}
      </div>

      {/* Main Content States */}
      {loading ? (
        <Loading message="Loading floats directory..." />
      ) : error ? (
        <div className="p-4">
          <ErrorState message={error} onRetry={onRetry} />
        </div>
      ) : floats.length === 0 ? (
        <EmptyState title="No floats found" message="No ARGO floats match the search criteria." />
      ) : (
        <div className="overflow-x-auto w-full">
          <table className="w-full text-left text-xs min-w-[700px]">
            <thead className="bg-surface-1 border-b border-subtle text-secondary uppercase tracking-wider font-mono text-[11px]">
              <tr>
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
                  onClick={() => handleSort("profile_count")}
                  className="px-4 py-3 cursor-pointer hover:text-primary transition-colors select-none font-medium text-right"
                >
                  <div className="flex items-center justify-end">
                    <span>Profiles Recorded</span>
                    {renderSortIcon("profile_count")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("min_time")}
                  className="px-4 py-3 cursor-pointer hover:text-primary transition-colors select-none font-medium"
                >
                  <div className="flex items-center">
                    <span>First Observation</span>
                    {renderSortIcon("min_time")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("max_time")}
                  className="px-4 py-3 cursor-pointer hover:text-primary transition-colors select-none font-medium"
                >
                  <div className="flex items-center">
                    <span>Latest Observation</span>
                    {renderSortIcon("max_time")}
                  </div>
                </th>
                {projectsRecorded && (
                  <>
                    <th className="px-4 py-3 font-medium">Project</th>
                    <th className="px-4 py-3 font-medium">Platform</th>
                  </>
                )}
                <th className="px-4 py-3 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-subtle text-primary">
              {floats.map((f) => (
                <tr key={f.float_id} className="hover:bg-surface-1/60 transition-colors">
                  <td className="px-4 py-3 font-mono font-medium text-accent">
                    {f.wmo_number}
                  </td>
                  <td className="px-4 py-3 font-mono font-medium text-primary text-right tabular-nums">
                    {f.profile_count}
                  </td>
                  <td className="px-4 py-3 font-mono text-secondary">
                    {formatDate(f.min_time)}
                  </td>
                  <td className="px-4 py-3 font-mono text-secondary">
                    {formatDate(f.max_time)}
                  </td>
                  {projectsRecorded && (
                    <>
                      <td className="px-4 py-3 font-mono text-secondary">
                        {f.project_name && f.project_name !== "N/A" ? (
                          f.project_name
                        ) : (
                          <span className="text-muted">—</span>
                        )}
                      </td>
                      <td className="px-4 py-3 font-mono text-secondary">
                        {f.platform_type && f.platform_type !== "N/A" ? (
                          f.platform_type
                        ) : (
                          <span className="text-muted">—</span>
                        )}
                      </td>
                    </>
                  )}
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => onSelectWmo(f.wmo_number)}
                      className="inline-flex items-center gap-1.5 px-3 py-1 bg-surface-1 hover:bg-surface-2 border border-subtle text-primary rounded-[2px] text-xs font-mono transition-colors cursor-pointer"
                    >
                      <FiList className="text-xs" />
                      <span>View Profiles</span>
                    </button>
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

export default FloatsTable;
