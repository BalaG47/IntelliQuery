import React, { useRef, useEffect, useState } from "react";
import { Line } from "react-chartjs-2";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
} from "chart.js";
import Globe from "react-globe.gl";
import { useTheme } from "../context/ThemeContext";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend);

const Visualization = () => {
  const containerRef = useRef(null);
  const [width, setWidth] = useState(0);
  const { isDark } = useTheme();

  useEffect(() => {
    const handleResize = () => {
      if (containerRef.current) setWidth(containerRef.current.offsetWidth);
    };
    handleResize();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  // Approved Theme Colors:
  // Light: Deck rust #96320C, grid #DDD7CB, text #141C24
  // Dark:  Buoy rust #E58532, grid #223344, text #E5EDF5
  const seriesColor = isDark ? "#E58532" : "#96320C";
  const seriesBg = isDark ? "rgba(229, 133, 50, 0.15)" : "rgba(150, 50, 12, 0.15)";
  const gridColor = isDark ? "#223344" : "#DDD7CB";
  const textColor = isDark ? "#E5EDF5" : "#141C24";
  const mutedTextColor = isDark ? "#8BA2B6" : "#4F5E6E";
  const tooltipBg = isDark ? "#17232E" : "#FAF8F5";

  const lineData = {
    labels: ["Jul 2019", "Jul 2020", "Jul 2021", "Jul 2022", "Jul 2023", "Jul 2024"],
    datasets: [
      {
        label: "Temperature Anomaly (°C)",
        data: [0.5, 0.7, 0.8, 0.85, 0.95, 1.1],
        borderColor: seriesColor,
        backgroundColor: seriesBg,
        tension: 0.3,
        pointBackgroundColor: seriesColor,
        pointBorderColor: isDark ? "#0B1218" : "#FAF8F5",
        pointBorderWidth: 1.5,
      },
    ],
  };

  const lineOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        display: true,
        position: "bottom",
        labels: {
          color: textColor,
          font: { family: "IBM Plex Mono, monospace", size: 11 },
          boxWidth: 12,
        },
      },
      title: {
        display: true,
        text: "Global Sea Surface Temperature Anomaly (2019-2024)",
        color: textColor,
        font: { family: "Chivo, sans-serif", size: 13, weight: "bold" },
        padding: { top: 4, bottom: 12 },
      },
      tooltip: {
        backgroundColor: tooltipBg,
        titleColor: textColor,
        bodyColor: textColor,
        borderColor: gridColor,
        borderWidth: 1,
        bodyFont: { family: "IBM Plex Mono, monospace" },
        titleFont: { family: "Chivo, sans-serif" },
      },
    },
    scales: {
      x: {
        grid: { color: gridColor },
        ticks: { color: mutedTextColor, font: { family: "IBM Plex Mono, monospace", size: 10 } },
      },
      y: {
        grid: { color: gridColor },
        ticks: { color: mutedTextColor, font: { family: "IBM Plex Mono, monospace", size: 10 } },
      },
    },
  };

  return (
    <div ref={containerRef} className="space-y-4 w-full">
      {/* Line Chart Panel */}
      <div className="bg-surface-0 border border-subtle rounded-[3px] p-3 text-primary" style={{ height: "280px" }}>
        <Line data={lineData} options={lineOptions} />
      </div>

      {/* 3D Globe Instrument Panel */}
      <div className="bg-surface-0 border border-subtle rounded-[3px] overflow-hidden p-2 flex flex-col items-center justify-center" style={{ height: "360px", width: "100%" }}>
        <div className="w-full flex items-center justify-between px-2 mb-1 text-[10px] font-mono uppercase text-muted">
          <span>Global Sphere Projection</span>
          <span className="text-accent">ARGO Floats Orbit</span>
        </div>
        {width > 0 && (
          <Globe
            width={Math.max(width - 24, 260)}
            height={320}
            globeImageUrl="//unpkg.com/three-globe/example/img/earth-dark.jpg"
            backgroundColor="rgba(0,0,0,0)"
            arcsData={[]}
          />
        )}
      </div>
    </div>
  );
};

export default Visualization;
