import React from "react";

const Loading = ({ message = "Loading data..." }) => {
  return (
    <div className="flex flex-col items-center justify-center p-12 bg-[#FAF8F5] dark:bg-[#111A22] rounded-[3px] border border-[#DDD7CB] dark:border-[#223344] text-center">
      <div className="w-8 h-8 border-2 border-[#DDD7CB] dark:border-[#223344] border-t-[#0E5E6F] dark:border-t-[#48C9A6] rounded-full animate-spin mb-3" />
      <p className="text-xs font-mono text-[#4F5E6E] dark:text-[#8BA2B6] tracking-wide uppercase">
        {message}
      </p>
    </div>
  );
};

export default Loading;
