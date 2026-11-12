import React from "react";
import { FiAlertTriangle, FiRefreshCw } from "react-icons/fi";

const ErrorState = ({ message = "Failed to load data.", onRetry }) => {
  return (
    <div className="flex flex-col items-center justify-center p-8 bg-[#FAF8F5] dark:bg-[#111A22] rounded-[3px] border border-[#992224]/50 dark:border-[#F46868]/50 text-center">
      <div className="w-10 h-10 rounded-[3px] bg-[#992224]/10 dark:bg-[#F46868]/15 text-[#992224] dark:text-[#F46868] flex items-center justify-center mb-3 text-lg border border-[#992224]/30 dark:border-[#F46868]/30">
        <FiAlertTriangle />
      </div>
      <h3 className="text-sm font-semibold text-[#992224] dark:text-[#F46868] mb-1">
        Telemetry Error
      </h3>
      <p className="text-xs text-[#4F5E6E] dark:text-[#8BA2B6] max-w-lg mb-4 font-mono">
        {message}
      </p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-[#992224] hover:bg-[#801B1D] dark:bg-[#F46868] dark:hover:bg-[#FF8080] text-white dark:text-[#071A15] text-xs font-mono font-medium rounded-[3px] uppercase tracking-wider transition-colors duration-100 cursor-pointer"
        >
          <FiRefreshCw className="text-xs" />
          <span>Retry</span>
        </button>
      )}
    </div>
  );
};

export default ErrorState;
