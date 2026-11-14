import React from "react";
import { FiInbox } from "react-icons/fi";

const EmptyState = ({
  title = "No records found",
  message = "No data matches your active filter criteria.",
  onClear,
}) => {
  return (
    <div className="flex flex-col items-center justify-center p-12 bg-surface-0 rounded-[3px] border border-subtle text-center">
      <div className="w-10 h-10 rounded-[3px] bg-surface-1 border border-subtle flex items-center justify-center text-secondary mb-3 text-lg">
        <FiInbox />
      </div>
      <h3 className="text-sm font-semibold text-primary mb-1">
        {title}
      </h3>
      <p className="text-xs text-muted max-w-md mb-4">
        {message}
      </p>
      {onClear && (
        <button
          type="button"
          onClick={onClear}
          className="px-3.5 py-1.5 bg-accent hover:opacity-90 text-accent-contrast text-xs font-mono font-medium rounded-[2px] uppercase tracking-wider transition-colors cursor-pointer"
        >
          Reset Filters
        </button>
      )}
    </div>
  );
};

export default EmptyState;
