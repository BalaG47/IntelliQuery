import React, { useState, useEffect, useRef, useCallback } from "react";
import Plot from "react-plotly.js";
import { getProfileDetail, extractErrorMessage } from "../../api";
import Loading from "./Loading";
import ErrorState from "./ErrorState";
import { FiX, FiAlertCircle } from "react-icons/fi";

// Standard oceanographic ranges (display-only, never used to drop points)
const STD_RANGE = { temperature: [-2, 35], salinity: [28, 40] };

/** Count non-null values in an array */
const countValid = (arr) => (arr ? arr.filter((v) => v !== null).length : 0);

/** Count points outside a [lo, hi] range, skipping nulls */
const countOutside = (arr, lo, hi) =>
  arr ? arr.filter((v) => v !== null && (v < lo || v > hi)).length : 0;

/** Format a date string for display */
const fmtDate = (iso) => (iso ? iso.replace("T", " ").slice(0, 19) : "—");

/** Format a number to 4 decimal places or "—" for null */
const fmtNum = (v, dec = 4) => (v === null || v === undefined ? "—" : Number(v).toFixed(dec));

// Chart parameter configurations
const CHART_PARAMS = [
  { key: "temperature", label: "Temperature", unit: "°C", color: "#f97316" },
  { key: "salinity", label: "Salinity", unit: "PSU", color: "#22d3ee" },
];

/**
 * Build a Plotly trace for a T/S vs Pressure chart.
 * Null values are passed as null so connectgaps=false creates gaps.
 */
const buildTSTrace = (measurements, param, color) => {
  const pressure = measurements.pressure;
  const values = measurements[param];
  if (!pressure || !values) return null;

  return {
    x: values.map((v) => (v === null ? null : v)),
    y: pressure.map((v) => (v === null ? null : v)),
    type: "scatter",
    mode: "lines+markers",
    connectgaps: false,
    line: { color, width: 1.5 },
    marker: { color, size: 4 },
    hovertemplate: `<b>${param}</b>: %{x}<br><b>Pressure</b>: %{y} dbar<extra></extra>`,
  };
};

/**
 * Build a Plotly trace for Pressure vs Level Index.
 * Null pressures produce gaps.
 */
const buildLevelTrace = (measurements) => {
  const pressure = measurements.pressure;
  if (!pressure) return null;
  const indices = pressure.map((_, i) => i);

  return {
    x: indices,
    y: pressure.map((v) => (v === null ? null : v)),
    type: "scatter",
    mode: "lines+markers",
    connectgaps: false,
    line: { color: "#a78bfa", width: 1.5 },
    marker: { color: "#a78bfa", size: 4 },
    hovertemplate: "<b>Level</b>: %{x}<br><b>Pressure</b>: %{y} dbar<extra></extra>",
  };
};

/** Shared Plotly layout base */
const baseLayout = {
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(15,20,35,0.9)",
  font: { color: "#BDC1CA", size: 11 },
  margin: { l: 60, r: 20, t: 32, b: 48 },
  xaxis: { gridcolor: "#2a3040", zerolinecolor: "#3a4050" },
  yaxis: { gridcolor: "#2a3040", zerolinecolor: "#3a4050", autorange: "reversed" },
  hoverlabel: { bgcolor: "#1E2128", font: { color: "#fff" } },
};

const plotConfig = { displayModeBar: false, responsive: true };

/** Single T or S vs Pressure chart */
const TSChart = ({ measurements, param, unit, color, label, rangeOn }) => {
  const trace = buildTSTrace(measurements, param, color);
  if (!trace) return <ChartEmpty label={label} />;

  const validCount = countValid(measurements[param]);
  if (validCount === 0) return <ChartEmpty label={label} message="No valid data for this parameter." />;

  const outsideCount = rangeOn ? countOutside(measurements[param], STD_RANGE[param][0], STD_RANGE[param][1]) : 0;

  const layout = {
    ...baseLayout,
    title: { text: `${label} vs Pressure`, font: { color: "#e2e8f0", size: 13 } },
    xaxis: {
      ...baseLayout.xaxis,
      title: { text: `${label} (${unit})`, standoff: 8 },
      range: rangeOn ? STD_RANGE[param] : undefined,
      autorange: rangeOn ? false : true,
    },
    yaxis: {
      ...baseLayout.yaxis,
      title: { text: "Pressure (dbar)", standoff: 8 },
      autorange: "reversed",
    },
  };

  return (
    <div>
      <Plot
        data={[trace]}
        layout={layout}
        config={plotConfig}
        style={{ width: "100%", height: 340 }}
        useResizeHandler
      />
      {rangeOn && (
        <p className="text-xs text-amber-300/80 mt-1 text-center">
          {outsideCount} point{outsideCount !== 1 ? "s" : ""} outside standard range (
          {STD_RANGE[param][0]} – {STD_RANGE[param][1]} {unit})
        </p>
      )}
    </div>
  );
};

/** Pressure vs Level Index chart */
const LevelChart = ({ measurements }) => {
  const trace = buildLevelTrace(measurements);
  if (!trace) return <ChartEmpty label="Pressure vs Level" />;

  const layout = {
    ...baseLayout,
    title: { text: "Pressure vs Level Index", font: { color: "#e2e8f0", size: 13 } },
    xaxis: {
      ...baseLayout.xaxis,
      title: { text: "Level Index", standoff: 8 },
    },
    yaxis: {
      ...baseLayout.yaxis,
      title: { text: "Pressure (dbar)", standoff: 8 },
      autorange: "reversed",
    },
  };

  return (
    <Plot
      data={[trace]}
      layout={layout}
      config={plotConfig}
      style={{ width: "100%", height: 340 }}
      useResizeHandler
    />
  );
};

/** Fallback when a chart has no data */
const ChartEmpty = ({ label, message }) => (
  <div className="flex items-center justify-center h-40 rounded-lg bg-gray-900/60 border border-gray-700/50 text-gray-500 text-sm">
    {message || `No data for ${label}.`}
  </div>
);

/** Measurement table with sticky header and null handling */
const MeasurementTable = ({ measurements }) => {
  const { pressure, temperature, salinity } = measurements;
  const levels = pressure ? pressure.length : 0;

  return (
    <div className="overflow-auto max-h-72 rounded-lg border border-gray-700/60">
      <table className="w-full text-xs text-left">
        <thead className="sticky top-0 bg-gray-900 border-b border-gray-700 text-[#BDC1CA] uppercase tracking-wider">
          <tr>
            <th className="px-3 py-2">Level</th>
            <th className="px-3 py-2">Pressure (dbar)</th>
            <th className="px-3 py-2">Temperature (°C)</th>
            <th className="px-3 py-2">Salinity (PSU)</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800 text-gray-300">
          {Array.from({ length: levels }).map((_, i) => {
            const p = pressure[i];
            const t = temperature ? temperature[i] : undefined;
            const s = salinity ? salinity[i] : undefined;
            const allNull = p === null && (t === undefined || t === null) && (s === undefined || s === null);
            return (
              <tr
                key={i}
                className={`hover:bg-gray-800/50 transition ${allNull ? "opacity-40" : ""}`}
              >
                <td className="px-3 py-1.5 font-mono text-gray-400">{i}</td>
                <td className="px-3 py-1.5 font-mono">{p === null ? "—" : fmtNum(p, 3)}</td>
                <td className="px-3 py-1.5 font-mono">{t === null || t === undefined ? "—" : fmtNum(t, 3)}</td>
                <td className="px-3 py-1.5 font-mono">{s === null || s === undefined ? "—" : fmtNum(s, 3)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};

// View modes
const VIEW_MODES = [
  { id: "temperature", label: "Temperature" },
  { id: "salinity", label: "Salinity" },
  { id: "level", label: "Pressure vs Level" },
  { id: "side-by-side", label: "Side by Side" },
];

/**
 * ProfileDetailDrawer — slide-over panel showing profile metadata,
 * measurement table, and scientific charts.
 *
 * Props:
 *   profileId  {number|null}  — null = closed
 *   onClose    {function}     — called when user closes the drawer
 */
const ProfileDetailDrawer = ({ profileId, onClose }) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [viewMode, setViewMode] = useState("temperature");
  const [rangeOn, setRangeOn] = useState(false);
  const abortRef = useRef(null);

  const loadProfile = useCallback(async (id) => {
    if (abortRef.current) abortRef.current.abort();
    abortRef.current = new AbortController();

    setData(null);
    setError(null);
    setLoading(true);

    try {
      const res = await getProfileDetail(id, abortRef.current.signal);
      setData(res.profile);
    } catch (err) {
      const msg = extractErrorMessage(err);
      if (msg) setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (profileId !== null) {
      setRangeOn(false);
      setViewMode("temperature");
      loadProfile(profileId);
    }
    return () => {
      if (abortRef.current) abortRef.current.abort();
    };
  }, [profileId, loadProfile]);

  // Trap Escape key to close
  useEffect(() => {
    const handler = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  if (profileId === null) return null;

  const isOpen = profileId !== null;

  return (
    <>
      {/* Backdrop */}
      <div
        className={`fixed inset-0 z-40 bg-black/50 backdrop-blur-sm transition-opacity ${
          isOpen ? "opacity-100" : "opacity-0 pointer-events-none"
        }`}
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Drawer */}
      <aside
        className={`fixed right-0 top-0 h-full z-50 w-full max-w-2xl bg-[#0d1117] border-l border-[#FF1F4B33] shadow-2xl flex flex-col transition-transform duration-300 ${
          isOpen ? "translate-x-0" : "translate-x-full"
        }`}
        role="dialog"
        aria-modal="true"
        aria-label="Profile Detail"
      >
        {/* Drawer Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-800 bg-[#0d1117] shrink-0">
          <div>
            <h2 className="text-lg font-bold text-white">Profile Detail</h2>
            {data && (
              <p className="text-xs text-[#BDC1CA] mt-0.5">
                WMO {data.wmo_number} · Cycle {data.cycle_number}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-2 rounded-lg text-gray-400 hover:text-white hover:bg-gray-700/50 transition"
            aria-label="Close"
          >
            <FiX size={18} />
          </button>
        </div>

        {/* Drawer Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-6">
          {loading && <Loading message="Loading profile data..." />}

          {error && !loading && (
            <ErrorState
              message={error}
              onRetry={() => loadProfile(profileId)}
            />
          )}

          {data && !loading && (
            <>
              {/* QC Warning Banner */}
              <div className="flex items-start gap-2 p-3 rounded-lg bg-amber-950/40 border border-amber-700/40 text-amber-200/90 text-xs">
                <FiAlertCircle className="text-amber-400 mt-0.5 shrink-0" size={14} />
                <span>Raw, unadjusted measurements. No QC flags available.</span>
              </div>

              {/* Metadata */}
              <section>
                <h3 className="text-xs font-semibold text-[#BDC1CA] uppercase tracking-wider mb-3">
                  Metadata
                </h3>
                <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
                  <MetaRow label="Profile ID" value={`#${data.profile_id}`} mono />
                  <MetaRow label="Float ID" value={`#${data.float_id}`} mono />
                  <MetaRow label="WMO Number" value={data.wmo_number} mono />
                  <MetaRow label="Cycle Number" value={data.cycle_number} mono />
                  <MetaRow label="Observation Date" value={fmtDate(data.profile_time)} />
                  <MetaRow label="Latitude" value={fmtNum(data.latitude, 4) + " °N"} mono />
                  <MetaRow label="Longitude" value={fmtNum(data.longitude, 4) + " °E"} mono />
                  {data.data_mode !== undefined && data.data_mode !== null && (
                    <MetaRow label="Data Mode" value={data.data_mode} />
                  )}
                  {data.direction !== undefined && data.direction !== null && (
                    <MetaRow label="Direction" value={data.direction} />
                  )}
                </div>
              </section>

              {/* Summary */}
              {data.measurements && (
                <SummaryLine measurements={data.measurements} levelCount={data.level_count} />
              )}

              {/* Range Toggle */}
              {data.measurements && (
                <div className="flex items-center gap-3 text-sm">
                  <button
                    role="switch"
                    aria-checked={rangeOn}
                    onClick={() => setRangeOn((v) => !v)}
                    className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors focus:outline-none ${
                      rangeOn ? "bg-blue-600" : "bg-gray-700"
                    }`}
                  >
                    <span
                      className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform ${
                        rangeOn ? "translate-x-4" : "translate-x-1"
                      }`}
                    />
                  </button>
                  <span className="text-[#BDC1CA] text-xs">
                    Standard range (T: −2–35 °C, S: 28–40 PSU) — display only, no points removed
                  </span>
                </div>
              )}

              {/* Measurement Table */}
              {data.measurements && (
                <section>
                  <h3 className="text-xs font-semibold text-[#BDC1CA] uppercase tracking-wider mb-3">
                    Measurements
                  </h3>
                  <MeasurementTable measurements={data.measurements} />
                </section>
              )}

              {/* Charts */}
              {data.measurements && (
                <section>
                  <div className="flex flex-wrap items-center gap-2 mb-4">
                    <h3 className="text-xs font-semibold text-[#BDC1CA] uppercase tracking-wider mr-2">
                      Charts
                    </h3>
                    {VIEW_MODES.map((m) => (
                      <button
                        key={m.id}
                        onClick={() => setViewMode(m.id)}
                        className={`px-3 py-1 text-xs rounded-md border transition ${
                          viewMode === m.id
                            ? "bg-blue-600 border-blue-500 text-white"
                            : "bg-gray-800/60 border-gray-700 text-[#BDC1CA] hover:border-gray-500 hover:text-white"
                        }`}
                      >
                        {m.label}
                      </button>
                    ))}
                  </div>

                  {viewMode === "temperature" && (
                    <TSChart
                      measurements={data.measurements}
                      param="temperature"
                      unit="°C"
                      color="#f97316"
                      label="Temperature"
                      rangeOn={rangeOn}
                    />
                  )}
                  {viewMode === "salinity" && (
                    <TSChart
                      measurements={data.measurements}
                      param="salinity"
                      unit="PSU"
                      color="#22d3ee"
                      label="Salinity"
                      rangeOn={rangeOn}
                    />
                  )}
                  {viewMode === "level" && (
                    <LevelChart measurements={data.measurements} />
                  )}
                  {viewMode === "side-by-side" && (
                    <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                      <TSChart
                        measurements={data.measurements}
                        param="temperature"
                        unit="°C"
                        color="#f97316"
                        label="Temperature"
                        rangeOn={rangeOn}
                      />
                      <TSChart
                        measurements={data.measurements}
                        param="salinity"
                        unit="PSU"
                        color="#22d3ee"
                        label="Salinity"
                        rangeOn={rangeOn}
                      />
                    </div>
                  )}
                </section>
              )}
            </>
          )}
        </div>
      </aside>
    </>
  );
};

/** Single metadata key-value row */
const MetaRow = ({ label, value, mono }) => (
  <div>
    <span className="text-[#BDC1CA] text-xs">{label}</span>
    <p className={`text-white text-sm mt-0.5 ${mono ? "font-mono" : ""}`}>{value ?? "—"}</p>
  </div>
);

/** Summary line: total, valid per param, null count */
const SummaryLine = ({ measurements, levelCount }) => {
  const { pressure, temperature, salinity } = measurements;
  const total = levelCount ?? (pressure ? pressure.length : 0);
  const validP = countValid(pressure);
  const validT = countValid(temperature);
  const validS = countValid(salinity);
  const nullCount = pressure ? pressure.filter((v) => v === null).length : 0;

  return (
    <div className="text-xs text-[#BDC1CA] flex flex-wrap gap-x-4 gap-y-1 bg-gray-900/40 border border-gray-800 rounded-lg px-4 py-2.5">
      <span>Total levels: <strong className="text-white">{total}</strong></span>
      <span>Valid pressure: <strong className="text-white">{validP}</strong></span>
      <span>Valid temperature: <strong className="text-white">{validT}</strong></span>
      <span>Valid salinity: <strong className="text-white">{validS}</strong></span>
      {nullCount > 0 && (
        <span className="text-amber-300/70">Null levels: <strong>{nullCount}</strong></span>
      )}
    </div>
  );
};

export default ProfileDetailDrawer;
