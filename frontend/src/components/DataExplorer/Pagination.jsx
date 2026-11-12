import React from "react";
import { FiChevronLeft, FiChevronRight } from "react-icons/fi";

const Pagination = ({ page, totalPages, total, limit, onPageChange, loading }) => {
  if (total === 0 || totalPages <= 1) {
    if (total > 0) {
      return (
        <div className="flex justify-between items-center px-4 py-2.5 text-xs font-mono text-[#4F5E6E] dark:text-[#8BA2B6] border-t border-[#DDD7CB] dark:border-[#223344] bg-[#FAF8F5] dark:bg-[#111A22]">
          <span>Showing all {total} records</span>
          <span>Page 1 of 1</span>
        </div>
      );
    }
    return null;
  }

  const startIdx = (page - 1) * limit + 1;
  const endIdx = Math.min(page * limit, total);

  return (
    <div className="flex flex-col sm:flex-row items-center justify-between gap-3 px-4 py-2.5 border-t border-[#DDD7CB] dark:border-[#223344] bg-[#FAF8F5] dark:bg-[#111A22] text-xs font-mono text-[#4F5E6E] dark:text-[#8BA2B6]">
      <div>
        Showing <span className="text-[#141C24] dark:text-[#E5EDF5] font-semibold">{startIdx}</span> to{" "}
        <span className="text-[#141C24] dark:text-[#E5EDF5] font-semibold">{endIdx}</span> of{" "}
        <span className="text-[#141C24] dark:text-[#E5EDF5] font-semibold">{total.toLocaleString()}</span> entries
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1 || loading}
          className="inline-flex items-center gap-1 px-2.5 py-1 rounded-[3px] border border-[#DDD7CB] dark:border-[#223344] bg-[#EBE5DA] dark:bg-[#17232E] text-[#141C24] dark:text-[#E5EDF5] hover:bg-[#DFD7C8] dark:hover:bg-[#1F2E3D] hover:border-[#786C5A] dark:hover:border-[#60809E] disabled:opacity-40 disabled:cursor-not-allowed transition-colors duration-100 cursor-pointer"
        >
          <FiChevronLeft className="text-xs" />
          <span>Previous</span>
        </button>

        <span className="px-2 text-[#141C24] dark:text-[#E5EDF5] font-semibold">
          Page {page} / {totalPages}
        </span>

        <button
          type="button"
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages || loading}
          className="inline-flex items-center gap-1 px-2.5 py-1 rounded-[3px] border border-[#DDD7CB] dark:border-[#223344] bg-[#EBE5DA] dark:bg-[#17232E] text-[#141C24] dark:text-[#E5EDF5] hover:bg-[#DFD7C8] dark:hover:bg-[#1F2E3D] hover:border-[#786C5A] dark:hover:border-[#60809E] disabled:opacity-40 disabled:cursor-not-allowed transition-colors duration-100 cursor-pointer"
        >
          <span>Next</span>
          <FiChevronRight className="text-xs" />
        </button>
      </div>
    </div>
  );
};

export default Pagination;
