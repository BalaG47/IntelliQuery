import React from "react";
import { FiAlertTriangle, FiRefreshCw } from "react-icons/fi";

const ErrorState = ({ message = "Failed to load data.", onRetry }) => {
  return (
    <div className="flex flex-col items-center justify-center p-8 bg-surface-0 rounded-[3px] border border-state-error/40 text-center">
      <div className="w-10 h-10 rounded-[3px] bg-surface-1 text-state-error flex items-center justify-center mb-3 text-lg border border-state-error/30">
        <FiAlertTriangle />
      </div>
      <h3 className="text-sm font-semibold text-state-error mb-1 font-heading">
        Telemetry Error
      </h3>
      <p className="text-xs text-muted max-w-lg mb-4 font-mono">
        {message}
      </p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-state-error hover:opacity-90 text-white dark:text-[#071A15] text-xs font-mono font-medium rounded-[2px] uppercase tracking-wider transition-colors cursor-pointer"
        >
          <FiRefreshCw className="text-xs" />
          <span>Retry</span>
        </button>
      )}
    </div>
  );
};

export default ErrorState;
