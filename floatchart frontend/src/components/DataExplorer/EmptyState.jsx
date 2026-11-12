import React from "react";
import { FiInbox } from "react-icons/fi";

const EmptyState = ({
  title = "No records found",
  message = "No data matches your active filter criteria.",
  onClear,
}) => {
  return (
    <div className="flex flex-col items-center justify-center p-12 bg-[#FAF8F5] dark:bg-[#111A22] rounded-[3px] border border-[#DDD7CB] dark:border-[#223344] text-center">
      <div className="w-10 h-10 rounded-[3px] bg-[#EBE5DA] dark:bg-[#17232E] border border-[#DDD7CB] dark:border-[#223344] flex items-center justify-center text-[#475569] dark:text-[#9AB0C4] mb-3 text-lg">
        <FiInbox />
      </div>
      <h3 className="text-sm font-semibold text-[#141C24] dark:text-[#E5EDF5] mb-1">
        {title}
      </h3>
      <p className="text-xs text-[#4F5E6E] dark:text-[#8BA2B6] max-w-md mb-4">
        {message}
      </p>
      {onClear && (
        <button
          type="button"
          onClick={onClear}
          className="px-3.5 py-1.5 bg-[#0E5E6F] hover:bg-[#094754] dark:bg-[#48C9A6] dark:hover:bg-[#5CE0BD] text-white dark:text-[#071A15] text-xs font-mono font-medium rounded-[3px] uppercase tracking-wider transition-colors duration-100 cursor-pointer"
        >
          Reset Filters
        </button>
      )}
    </div>
  );
};

export default EmptyState;
