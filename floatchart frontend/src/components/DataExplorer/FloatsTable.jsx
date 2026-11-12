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
    if (sortBy !== col) return <span className="opacity-30">↕</span>;
    return sortDir === "asc" ? <FiChevronUp className="text-blue-400" /> : <FiChevronDown className="text-blue-400" />;
  };

  const formatDate = (isoStr) => {
    if (!isoStr) return "--";
    return isoStr.replace("T", " ");
  };

  return (
    <div className="bg-[#1E2128B3] border border-[#FF1F4B33] rounded-xl shadow-lg overflow-hidden">
      {/* Table Header Note */}
      <div className="p-4 border-b border-gray-800 flex flex-col sm:flex-row sm:items-center justify-between gap-2">
        <div>
          <h3 className="text-base font-semibold text-white">ARGO Floats Directory</h3>
          <p className="text-xs text-[#BDC1CAFF]">
            Overview of registered ocean profiling floats and total cycles.
          </p>
        </div>
        {!projectsRecorded && (
          <div className="flex items-center gap-1.5 text-xs text-amber-300/80 bg-amber-950/40 border border-amber-800/40 px-2.5 py-1 rounded-md">
            <FiInfo className="text-amber-400 shrink-0" />
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
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="bg-gray-900/80 border-b border-gray-800 text-[#BDC1CAFF] uppercase tracking-wider font-medium">
              <tr>
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
                  onClick={() => handleSort("profile_count")}
                  className="px-4 py-3 cursor-pointer hover:text-white transition select-none"
                >
                  <div className="flex items-center gap-1.5">
                    <span>Profiles Recorded</span>
                    {renderSortIcon("profile_count")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("min_time")}
                  className="px-4 py-3 cursor-pointer hover:text-white transition select-none"
                >
                  <div className="flex items-center gap-1.5">
                    <span>First Observation</span>
                    {renderSortIcon("min_time")}
                  </div>
                </th>
                <th
                  onClick={() => handleSort("max_time")}
                  className="px-4 py-3 cursor-pointer hover:text-white transition select-none"
                >
                  <div className="flex items-center gap-1.5">
                    <span>Latest Observation</span>
                    {renderSortIcon("max_time")}
                  </div>
                </th>
                {projectsRecorded && (
                  <>
                    <th className="px-4 py-3">Project</th>
                    <th className="px-4 py-3">Platform</th>
                  </>
                )}
                <th className="px-4 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800 text-gray-200">
              {floats.map((f) => (
                <tr key={f.float_id} className="hover:bg-gray-800/50 transition">
                  <td className="px-4 py-3 font-mono font-semibold text-blue-300">
                    {f.wmo_number}
                  </td>
                  <td className="px-4 py-3 font-semibold text-white">
                    {f.profile_count}
                  </td>
                  <td className="px-4 py-3 font-mono text-gray-400">
                    {formatDate(f.min_time)}
                  </td>
                  <td className="px-4 py-3 font-mono text-gray-400">
                    {formatDate(f.max_time)}
                  </td>
                  {projectsRecorded && (
                    <>
                      <td className="px-4 py-3">{f.project_name || "N/A"}</td>
                      <td className="px-4 py-3">{f.platform_type || "N/A"}</td>
                    </>
                  )}
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => onSelectWmo(f.wmo_number)}
                      className="inline-flex items-center gap-1.5 px-3 py-1 bg-blue-600/80 hover:bg-blue-600 text-white rounded-md text-xs font-medium transition"
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
