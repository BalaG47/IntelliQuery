import React from "react";
import { FiChevronLeft, FiChevronRight } from "react-icons/fi";

const Pagination = ({ page, totalPages, total, limit, onPageChange, loading }) => {
  if (total === 0 || totalPages <= 1) {
    if (total > 0) {
      return (
        <div className="flex justify-between items-center px-4 py-2.5 text-xs font-mono text-muted border-t border-subtle bg-surface-0">
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
    <div className="flex flex-col sm:flex-row items-center justify-between gap-3 px-4 py-2.5 border-t border-subtle bg-surface-0 text-xs font-mono text-muted">
      <div>
        Showing <span className="text-primary font-semibold tabular-nums">{startIdx}</span> to{" "}
        <span className="text-primary font-semibold tabular-nums">{endIdx}</span> of{" "}
        <span className="text-primary font-semibold tabular-nums">{total.toLocaleString()}</span> entries
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1 || loading}
          className="inline-flex items-center gap-1 px-2.5 py-1 rounded-[2px] border border-subtle bg-surface-1 text-primary hover:bg-surface-2 disabled:opacity-40 disabled:cursor-not-allowed transition-colors cursor-pointer"
        >
          <FiChevronLeft className="text-xs" />
          <span>Previous</span>
        </button>

        <span className="px-2 text-primary font-semibold tabular-nums">
          Page {page} / {totalPages}
        </span>

        <button
          type="button"
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages || loading}
          className="inline-flex items-center gap-1 px-2.5 py-1 rounded-[2px] border border-subtle bg-surface-1 text-primary hover:bg-surface-2 disabled:opacity-40 disabled:cursor-not-allowed transition-colors cursor-pointer"
        >
          <span>Next</span>
          <FiChevronRight className="text-xs" />
        </button>
      </div>
    </div>
  );
};

export default Pagination;
