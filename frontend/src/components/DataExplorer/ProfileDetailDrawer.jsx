import React, { useState, useEffect, useRef, useCallback } from "react";
import Plot from "react-plotly.js";
import { getProfileDetail, extractErrorMessage } from "../../api";
import { useTheme } from "../../context/ThemeContext";
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
const buildLevelTrace = (measurements, color) => {
  const pressure = measurements.pressure;
  if (!pressure) return null;
  const indices = pressure.map((_, i) => i);

  return {
    x: indices,
    y: pressure.map((v) => (v === null ? null : v)),
    type: "scatter",
    mode: "lines+markers",
    connectgaps: false,
    line: { color, width: 1.5 },
    marker: { color, size: 4 },
    hovertemplate: "<b>Level</b>: %{x}<br><b>Pressure</b>: %{y} dbar<extra></extra>",
  };
};

const plotConfig = { displayModeBar: false, responsive: true };

/** Single T or S vs Pressure chart */
const TSChart = ({ measurements, param, unit, color, label, rangeOn, isDark }) => {
  const trace = buildTSTrace(measurements, param, color);
  if (!trace) return <ChartEmpty label={label} />;

  const validCount = countValid(measurements[param]);
  if (validCount === 0) return <ChartEmpty label={label} message="No valid data for this parameter." />;

  const outsideCount = rangeOn ? countOutside(measurements[param], STD_RANGE[param][0], STD_RANGE[param][1]) : 0;

  const fontColor = isDark ? "#E5EDF5" : "#141C24";
  const gridColor = isDark ? "#223344" : "#DDD7CB";
  const zeroColor = isDark ? "#60809E" : "#786C5A";
  const plotBg = isDark ? "rgba(17, 26, 34, 0.95)" : "rgba(250, 248, 245, 0.95)";
  const hoverBg = isDark ? "#17232E" : "#EBE5DA";

  const layout = {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: plotBg,
    font: { color: fontColor, size: 11, family: "IBM Plex Mono, monospace" },
    margin: { l: 60, r: 20, t: 32, b: 48 },
    title: { text: `${label} vs Pressure`, font: { color: fontColor, size: 13, family: "Chivo, sans-serif" } },
    xaxis: {
      gridcolor: gridColor,
      zerolinecolor: zeroColor,
      title: { text: `${label} (${unit})`, standoff: 8 },
      range: rangeOn ? STD_RANGE[param] : undefined,
      autorange: rangeOn ? false : true,
    },
    yaxis: {
      gridcolor: gridColor,
      zerolinecolor: zeroColor,
      title: { text: "Pressure (dbar)", standoff: 8 },
      autorange: "reversed",
    },
    hoverlabel: { bgcolor: hoverBg, font: { color: fontColor, family: "IBM Plex Mono, monospace" } },
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
        <p className="text-xs text-state-warning mt-1 text-center font-mono">
          {outsideCount} point{outsideCount !== 1 ? "s" : ""} outside standard range (
          {STD_RANGE[param][0]} – {STD_RANGE[param][1]} {unit})
        </p>
      )}
    </div>
  );
};

/** Pressure vs Level Index chart */
const LevelChart = ({ measurements, color, isDark }) => {
  const trace = buildLevelTrace(measurements, color);
  if (!trace) return <ChartEmpty label="Pressure vs Level" />;

  const fontColor = isDark ? "#E5EDF5" : "#141C24";
  const gridColor = isDark ? "#223344" : "#DDD7CB";
  const zeroColor = isDark ? "#60809E" : "#786C5A";
  const plotBg = isDark ? "rgba(17, 26, 34, 0.95)" : "rgba(250, 248, 245, 0.95)";
  const hoverBg = isDark ? "#17232E" : "#EBE5DA";

  const layout = {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: plotBg,
    font: { color: fontColor, size: 11, family: "IBM Plex Mono, monospace" },
    margin: { l: 60, r: 20, t: 32, b: 48 },
    title: { text: "Pressure vs Level Index", font: { color: fontColor, size: 13, family: "Chivo, sans-serif" } },
    xaxis: {
      gridcolor: gridColor,
      zerolinecolor: zeroColor,
      title: { text: "Level Index", standoff: 8 },
    },
    yaxis: {
      gridcolor: gridColor,
      zerolinecolor: zeroColor,
      title: { text: "Pressure (dbar)", standoff: 8 },
      autorange: "reversed",
    },
    hoverlabel: { bgcolor: hoverBg, font: { color: fontColor, family: "IBM Plex Mono, monospace" } },
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
  <div className="flex items-center justify-center h-40 rounded-[2px] bg-surface-1 border border-subtle text-muted text-xs font-mono">
    {message || `No data for ${label}.`}
  </div>
);

/** Measurement table with sticky header and null handling */
const MeasurementTable = ({ measurements }) => {
  const { pressure, temperature, salinity } = measurements;
  const levels = pressure ? pressure.length : 0;

  return (
    <div className="overflow-auto max-h-72 rounded-[2px] border border-subtle">
      <table className="w-full text-xs text-left">
        <thead className="sticky top-0 bg-surface-1 border-b border-subtle text-secondary font-mono uppercase tracking-wider text-[11px]">
          <tr>
            <th className="px-3 py-2 font-medium">Level</th>
            <th className="px-3 py-2 font-medium">Pressure (dbar)</th>
            <th className="px-3 py-2 font-medium">Temperature (°C)</th>
            <th className="px-3 py-2 font-medium">Salinity (PSU)</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-subtle text-primary">
          {Array.from({ length: levels }).map((_, i) => {
            const p = pressure[i];
            const t = temperature ? temperature[i] : undefined;
            const s = salinity ? salinity[i] : undefined;
            const allNull = p === null && (t === undefined || t === null) && (s === undefined || s === null);
            return (
              <tr
                key={i}
                className={`hover:bg-surface-1/60 transition-colors ${allNull ? "opacity-40" : ""}`}
              >
                <td className="px-3 py-1.5 font-mono text-muted tabular-nums">{i}</td>
                <td className="px-3 py-1.5 font-mono tabular-nums">{p === null ? <span className="text-muted">—</span> : fmtNum(p, 3)}</td>
                <td className="px-3 py-1.5 font-mono tabular-nums">{t === null || t === undefined ? <span className="text-muted">—</span> : fmtNum(t, 3)}</td>
                <td className="px-3 py-1.5 font-mono tabular-nums">{s === null || s === undefined ? <span className="text-muted">—</span> : fmtNum(s, 3)}</td>
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
 */
const ProfileDetailDrawer = ({ profileId, onClose }) => {
  const { isDark } = useTheme();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [viewMode, setViewMode] = useState("temperature");
  const [rangeOn, setRangeOn] = useState(false);
  const abortRef = useRef(null);

  // Approved data-series colors (separate from UI accent):
  // Light: Deck Rust #96320C, Oceanic Teal #186074, Slate #435266
  // Dark:  Buoy Rust #E58532, Cobalt Teal #3BA7C9, Slate #8E9EAF
  const tempColor = isDark ? "#E58532" : "#96320C";
  const salColor = isDark ? "#3BA7C9" : "#186074";
  const presColor = isDark ? "#8E9EAF" : "#435266";

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
    const handler = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  if (profileId === null) return null;

  const isOpen = profileId !== null;

  return (
    <>
      {/* Backdrop */}
      <div
        className={`fixed inset-0 z-40 bg-black/50 transition-opacity ${
          isOpen ? "opacity-100" : "opacity-0 pointer-events-none"
        }`}
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Drawer */}
      <aside
        className={`fixed right-0 top-0 h-full z-50 w-full max-w-2xl bg-surface-0 border-l border-subtle shadow-xl flex flex-col transition-transform duration-300 ${
          isOpen ? "translate-x-0" : "translate-x-full"
        }`}
        role="dialog"
        aria-modal="true"
        aria-label="Profile Detail"
      >
        {/* Drawer Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-subtle bg-surface-0 shrink-0">
          <div>
            <h2 className="text-lg font-bold text-primary font-heading">Profile Detail</h2>
            {data && (
              <p className="text-xs text-secondary mt-0.5 font-mono">
                WMO {data.wmo_number} · Cycle {data.cycle_number}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-[2px] text-muted hover:text-primary hover:bg-surface-1 transition-colors cursor-pointer"
            aria-label="Close"
          >
            <FiX size={18} />
          </button>
        </div>

        {/* Drawer Body */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-6">
          {loading && <Loading message="Loading profile data..." />}

          {error && !loading && (
            <ErrorState message={error} onRetry={() => loadProfile(profileId)} />
          )}

          {data && !loading && (
            <>
              {/* QC Warning Banner */}
              <div className="flex items-start gap-2 p-3 rounded-[2px] bg-surface-1 border border-state-warning/40 text-state-warning text-xs font-mono">
                <FiAlertCircle className="mt-0.5 shrink-0" size={14} />
                <span>Raw, unadjusted measurements. No QC flags available.</span>
              </div>

              {/* Metadata */}
              <section>
                <h3 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-3 font-mono">
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
                    type="button"
                    role="switch"
                    aria-checked={rangeOn}
                    onClick={() => setRangeOn((v) => !v)}
                    className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors cursor-pointer ${
                      rangeOn ? "bg-accent" : "bg-surface-2 border border-subtle"
                    }`}
                  >
                    <span
                      className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform ${
                        rangeOn ? "translate-x-4" : "translate-x-1"
                      }`}
                    />
                  </button>
                  <span className="text-secondary text-xs font-mono">
                    Standard range (T: −2–35 °C, S: 28–40 PSU) — display only, no points removed
                  </span>
                </div>
              )}

              {/* Measurement Table */}
              {data.measurements && (
                <section>
                  <h3 className="text-xs font-semibold text-secondary uppercase tracking-wider mb-3 font-mono">
                    Measurements
                  </h3>
                  <MeasurementTable measurements={data.measurements} />
                </section>
              )}

              {/* Charts */}
              {data.measurements && (
                <section>
                  <div className="flex flex-wrap items-center gap-2 mb-4">
                    <h3 className="text-xs font-semibold text-secondary uppercase tracking-wider mr-2 font-mono">
                      Charts
                    </h3>
                    {VIEW_MODES.map((m) => (
                      <button
                        key={m.id}
                        type="button"
                        onClick={() => setViewMode(m.id)}
                        className={`px-3 py-1 text-xs rounded-[2px] border font-mono transition-colors cursor-pointer ${
                          viewMode === m.id
                            ? "bg-accent border-accent text-accent-contrast font-medium"
                            : "bg-surface-1 border-subtle text-secondary hover:text-primary hover:bg-surface-2"
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
                      color={tempColor}
                      label="Temperature"
                      rangeOn={rangeOn}
                      isDark={isDark}
                    />
                  )}
                  {viewMode === "salinity" && (
                    <TSChart
                      measurements={data.measurements}
                      param="salinity"
                      unit="PSU"
                      color={salColor}
                      label="Salinity"
                      rangeOn={rangeOn}
                      isDark={isDark}
                    />
                  )}
                  {viewMode === "level" && (
                    <LevelChart
                      measurements={data.measurements}
                      color={presColor}
                      isDark={isDark}
                    />
                  )}
                  {viewMode === "side-by-side" && (
                    <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                      <TSChart
                        measurements={data.measurements}
                        param="temperature"
                        unit="°C"
                        color={tempColor}
                        label="Temperature"
                        rangeOn={rangeOn}
                        isDark={isDark}
                      />
                      <TSChart
                        measurements={data.measurements}
                        param="salinity"
                        unit="PSU"
                        color={salColor}
                        label="Salinity"
                        rangeOn={rangeOn}
                        isDark={isDark}
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
    <span className="text-muted text-xs font-mono">{label}</span>
    <p className={`text-primary text-sm mt-0.5 ${mono ? "font-mono tabular-nums" : ""}`}>
      {value ?? "—"}
    </p>
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
    <div className="text-xs text-secondary flex flex-wrap gap-x-4 gap-y-1 bg-surface-1 border border-subtle rounded-[2px] px-4 py-2.5 font-mono">
      <span>Total levels: <strong className="text-primary tabular-nums">{total}</strong></span>
      <span>Valid pressure: <strong className="text-primary tabular-nums">{validP}</strong></span>
      <span>Valid temperature: <strong className="text-primary tabular-nums">{validT}</strong></span>
      <span>Valid salinity: <strong className="text-primary tabular-nums">{validS}</strong></span>
      {nullCount > 0 && (
        <span className="text-state-warning">Null levels: <strong className="tabular-nums">{nullCount}</strong></span>
      )}
    </div>
  );
};

export default ProfileDetailDrawer;
