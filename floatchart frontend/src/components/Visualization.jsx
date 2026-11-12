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

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend);

const Visualization = () => {
  const containerRef = useRef(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const handleResize = () => {
      if (containerRef.current) setWidth(containerRef.current.offsetWidth);
    };
    handleResize();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  const lineData = {
    labels: ["Jul 2019", "Jul 2020", "Jul 2021", "Jul 2022", "Jul 2023", "Jul 2024"],
    datasets: [
      {
        label: "Temperature Anomaly (°C)",
        data: [0.5, 0.7, 0.8, 0.85, 0.95, 1.1],
        borderColor: "rgb(239, 68, 68)",
        backgroundColor: "rgba(239, 68, 68, 0.2)",
        tension: 0.3,
      },
    ],
  };

  const lineOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: true, position: "bottom" },
      title: { display: true, text: "Global Sea Surface Temperature Anomaly (2019-2024)" },
    },
  };

  return (
   <div ref={containerRef} className="space-y-6 w-full p-2 mt-11">
  {/* Line Chart */}
  <div className="bg-[#1E2128B3] text-white p-4 rounded-xl border-[1px] border-[#FF1F4B33] shadow-[0_0_2px_#171a1f26,0_0_2px_#171a1f33] " style={{ height: "300px" }}>
    <Line data={lineData} options={lineOptions} />
  </div>

  {/* 3D Globe */}
  <div className="bg-[#1E2128B3] rounded-xl shadow-[0_0_2px_#171a1f26,0_0_2px_#171a1f33]" style={{ height: "400px", width: "100%" }}>
    {width > 0 && (
      <Globe
        width={width}
        height={400}
        globeImageUrl="//unpkg.com/three-globe/example/img/earth-dark.jpg"
        arcsData={[]}
        arcColor={() => "#1E2128B3"}
      />
    )}
  </div>
</div>

  );
};

export default Visualization;
