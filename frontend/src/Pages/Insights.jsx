import React, { useState, useEffect, useCallback, useRef } from "react";
import Plot from "react-plotly.js";
import Navbar from "../components/Navbar";
import Sidebar from "../components/Sidebar";
import Loading from "../components/DataExplorer/Loading";
import ErrorState from "../components/DataExplorer/ErrorState";
import EmptyState from "../components/DataExplorer/EmptyState";
import { useTheme } from "../context/ThemeContext";
import {
  getInsightsSummary,
  getInsightsDepthProfile,
  getInsightsCompare,
  extractErrorMessage,
} from "../api";
import { FiActivity, FiLayers, FiSliders, FiGitMerge, FiAlertCircle } from "react-icons/fi";

const BGC_PARAMS = [
  "DOXY",
  "CHLA",
  "BBP700",
  "NITRATE",
  "PH_IN_SITU_TOTAL",
  "CHLA_FLUORESCENCE",
];

const COMPARE_PARAMS = [
  "DOXY",
  "CHLA",
  "BBP700",
  "NITRATE",
  "PH_IN_SITU_TOTAL",
  "CHLA_FLUORESCENCE",
  "TEMP",
  "PSAL",
];

const BIN_SIZES = [25, 50, 100, 200];

const fmtStat = (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(4));

export default function Insights() {
  const { isDark } = useTheme();

  // Section A & B: Summary state
  const [summary, setSummary] = useState(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState(null);

  // Section C: Depth-binned analysis state
  const [depthParam, setDepthParam] = useState("DOXY");
  const [binSize, setBinSize] = useState(50);
  const [depthWmo, setDepthWmo] = useState("");
  const [depthData, setDepthData] = useState(null);
  const [depthLoading, setDepthLoading] = useState(true);
  const [depthError, setDepthError] = useState(null);
  const depthAbortRef = useRef(null);

  // Section D: Profile comparison state
  const [compareIdsInput, setCompareIdsInput] = useState("");
  const [compareParam, setCompareParam] = useState("DOXY");
  const [compareData, setCompareData] = useState(null);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareError, setCompareError] = useState(null);
  const compareAbortRef = useRef(null);

  const fontColor = isDark ? "#E5EDF5" : "#141C24";
  const gridColor = isDark ? "#223344" : "#DDD7CB";
  const plotBg = isDark ? "rgba(17, 26, 34, 0.95)" : "rgba(250, 248, 245, 0.95)";
  const hoverBg = isDark ? "#17232E" : "#EBE5DA";
  const seriesColors = isDark
    ? ["#3BA7C9", "#E58532", "#7AC74F", "#C77DFF", "#FFD166"]
    : ["#186074", "#96320C", "#2B7A3D", "#6C3EB8", "#B57B00"];

  const loadSummary = useCallback(async () => {
    setSummaryLoading(true);
    setSummaryError(null);
    try {
      const res = await getInsightsSummary();
      setSummary(res);
    } catch (err) {
      const msg = extractErrorMessage(err);
      if (msg) setSummaryError(msg);
    } finally {
      setSummaryLoading(false);
    }
  }, []);

  const loadDepthProfile = useCallback(async () => {
    if (depthAbortRef.current) depthAbortRef.current.abort();
    depthAbortRef.current = new AbortController();
    setDepthLoading(true);
    setDepthError(null);
    try {
      const params = { parameter: depthParam, bin_size: binSize };
      if (depthWmo.trim()) params.wmo_number = depthWmo.trim();
      const res = await getInsightsDepthProfile(params, depthAbortRef.current.signal);
      setDepthData(res);
    } catch (err) {
      const msg = extractErrorMessage(err);
      if (msg) setDepthError(msg);
    } finally {
      setDepthLoading(false);
    }
  }, [depthParam, binSize, depthWmo]);

  useEffect(() => {
    loadSummary();
  }, [loadSummary]);

  useEffect(() => {
    loadDepthProfile();
    return () => {
      if (depthAbortRef.current) depthAbortRef.current.abort();
    };
  }, [loadDepthProfile]);

  const handleCompareSubmit = async (e) => {
    e.preventDefault();
    if (!compareIdsInput.trim()) {
      setCompareError("Enter 2 to 5 comma-separated Profile IDs to compare.");
      return;
    }
    if (compareAbortRef.current) compareAbortRef.current.abort();
    compareAbortRef.current = new AbortController();
    setCompareLoading(true);
    setCompareError(null);
    try {
      const res = await getInsightsCompare(
        { profile_ids: compareIdsInput.trim(), parameter: compareParam },
        compareAbortRef.current.signal
      );
      setCompareData(res);
    } catch (err) {
      const msg = extractErrorMessage(err);
      if (msg) setCompareError(msg);
    } finally {
      setCompareLoading(false);
    }
  };

  const composition = summary?.composition;
  const floatDist = summary?.float_distribution || [];
  const bgcStats = summary?.bgc_parameter_statistics || [];

  return (
    <div className="min-h-screen bg-app text-primary flex flex-col">
      <Navbar />
      <Sidebar />

      <main className="lg:ml-60 ml-0 flex-1 pt-20 pb-12 px-4 sm:px-6 lg:px-8 min-w-0">
        <div className="max-w-7xl w-full mx-auto space-y-6">
        {/* Page Header */}
        <div className="border-b border-subtle pb-4">
          <div className="flex items-center gap-2 text-accent text-xs font-mono uppercase tracking-widest mb-1">
            <FiActivity />
            <span>Scientific Insights</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-bold text-primary font-heading tracking-tight">
            ARGO Dataset Composition & BGC Analysis
          </h1>
          <p className="text-sm text-secondary mt-1 max-w-3xl">
            Validated statistics computed directly from MySQL measurements via{" "}
            <code className="font-mono text-xs bg-surface-1 px-1.5 py-0.5 rounded-[2px] border border-subtle">
              bgc_calculations.py
            </code>
            . Core and BGC pressure axes are kept strictly separate.
          </p>
        </div>

        {/* Section A & B: Summary + BGC Parameter Statistics */}
        {summaryLoading ? (
          <Loading message="Computing dataset composition and BGC statistics..." />
        ) : summaryError ? (
          <ErrorState message={summaryError} onRetry={loadSummary} />
        ) : (
          summary && (
            <>
              {/* A. Dataset Composition */}
              <section className="space-y-4">
                <div className="flex items-center gap-2 text-primary font-heading font-semibold text-base">
                  <FiLayers className="text-accent" />
                  <h2>Dataset Composition</h2>
                </div>

                <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                  <StatCard label="Total Floats" value={composition.total_floats} sub="WMO platforms" />
                  <StatCard label="Total Profiles" value={composition.total_profiles?.toLocaleString()} sub="All cycles" />
                  <StatCard label="BGC-Argo Profiles" value={composition.bgc_profiles?.toLocaleString()} sub="With BGC parameters" accent />
                  <StatCard label="Core-Only Profiles" value={composition.core_only_profiles?.toLocaleString()} sub="PRES / TEMP / PSAL" />
                </div>

                <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                  {/* Core vs BGC Composition Bar */}
                  <div className="bg-surface-0 border border-subtle rounded-[3px] p-4">
                    <Plot
                      data={[
                        {
                          x: ["Core-Only Profiles", "BGC-Argo Profiles"],
                          y: [composition.core_only_profiles, composition.bgc_profiles],
                          type: "bar",
                          marker: { color: [seriesColors[1], seriesColors[0]] },
                          hovertemplate: "<b>%{x}</b>: %{y} profiles<extra></extra>",
                        },
                      ]}
                      layout={{
                        paper_bgcolor: "rgba(0,0,0,0)",
                        plot_bgcolor: plotBg,
                        font: { color: fontColor, size: 11, family: "IBM Plex Mono, monospace" },
                        margin: { l: 50, r: 20, t: 36, b: 40 },
                        title: { text: "Core vs BGC Profile Distribution", font: { color: fontColor, size: 13, family: "Chivo, sans-serif" } },
                        xaxis: { gridcolor: gridColor },
                        yaxis: { gridcolor: gridColor, title: { text: "Profiles" } },
                        hoverlabel: { bgcolor: hoverBg, font: { color: fontColor } },
                      }}
                      config={{ displayModeBar: false, responsive: true }}
                      style={{ width: "100%", height: 280 }}
                      useResizeHandler
                    />
                  </div>

                  {/* Float Distribution Stacked Bar */}
                  <div className="bg-surface-0 border border-subtle rounded-[3px] p-4">
                    <Plot
                      data={[
                        {
                          x: floatDist.map((f) => String(f.wmo_number)),
                          y: floatDist.map((f) => f.bgc_profile_count),
                          name: "BGC Profiles",
                          type: "bar",
                          marker: { color: seriesColors[0] },
                        },
                        {
                          x: floatDist.map((f) => String(f.wmo_number)),
                          y: floatDist.map((f) => f.core_profile_count),
                          name: "Core-Only Profiles",
                          type: "bar",
                          marker: { color: seriesColors[1] },
                        },
                      ]}
                      layout={{
                        barmode: "stack",
                        paper_bgcolor: "rgba(0,0,0,0)",
                        plot_bgcolor: plotBg,
                        font: { color: fontColor, size: 10, family: "IBM Plex Mono, monospace" },
                        margin: { l: 50, r: 20, t: 36, b: 65 },
                        title: { text: "Profile Distribution by Float (WMO)", font: { color: fontColor, size: 13, family: "Chivo, sans-serif" } },
                        xaxis: { gridcolor: gridColor, tickangle: -45, type: "category" },
                        yaxis: { gridcolor: gridColor, title: { text: "Profiles" } },
                        legend: { orientation: "h", y: 1.12 },
                        hoverlabel: { bgcolor: hoverBg, font: { color: fontColor } },
                      }}
                      config={{ displayModeBar: false, responsive: true }}
                      style={{ width: "100%", height: 280 }}
                      useResizeHandler
                    />
                  </div>
                </div>
              </section>

              {/* B. BGC Parameter Insights */}
              <section className="bg-surface-0 border border-subtle rounded-[3px] overflow-hidden">
                <div className="p-4 border-b border-subtle">
                  <h2 className="text-base font-semibold text-primary font-heading">
                    BGC Parameter Summary Statistics
                  </h2>
                  <p className="text-xs text-secondary font-mono mt-1 flex items-center gap-1.5">
                    <FiAlertCircle className="text-accent shrink-0" />
                    <span>{summary.aggregation_note}</span>
                  </p>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs min-w-[820px]">
                    <thead className="bg-surface-1 border-b border-subtle text-secondary uppercase tracking-wider font-mono text-[11px]">
                      <tr>
                        <th className="px-4 py-2.5">Parameter</th>
                        <th className="px-4 py-2.5">Unit</th>
                        <th className="px-4 py-2.5 text-right">Profiles</th>
                        <th className="px-4 py-2.5 text-right">Valid Obs</th>
                        <th className="px-4 py-2.5 text-right">Missing</th>
                        <th className="px-4 py-2.5 text-right">Min</th>
                        <th className="px-4 py-2.5 text-right">Max</th>
                        <th className="px-4 py-2.5 text-right">Mean</th>
                        <th className="px-4 py-2.5 text-right">Median</th>
                        <th className="px-4 py-2.5 text-right">Std Dev</th>
                        <th className="px-4 py-2.5">QC Policy</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-subtle font-mono">
                      {bgcStats.map((st) => (
                        <tr key={st.parameter} className="hover:bg-surface-1/60">
                          <td className="px-4 py-2.5 font-semibold text-accent">{st.parameter}</td>
                          <td className="px-4 py-2.5 text-secondary">{st.unit}</td>
                          <td className="px-4 py-2.5 text-right tabular-nums">{st.profile_count}</td>
                          <td className="px-4 py-2.5 text-right tabular-nums">{st.valid_count?.toLocaleString()}</td>
                          <td className="px-4 py-2.5 text-right tabular-nums text-muted">{st.missing_count?.toLocaleString()}</td>
                          <td className="px-4 py-2.5 text-right tabular-nums">{fmtStat(st.minimum)}</td>
                          <td className="px-4 py-2.5 text-right tabular-nums">{fmtStat(st.maximum)}</td>
                          <td className="px-4 py-2.5 text-right tabular-nums">{fmtStat(st.mean)}</td>
                          <td className="px-4 py-2.5 text-right tabular-nums">{fmtStat(st.median)}</td>
                          <td className="px-4 py-2.5 text-right tabular-nums">{fmtStat(st.std_dev)}</td>
                          <td className="px-4 py-2.5 text-secondary text-[11px]">{st.qc_policy}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            </>
          )
        )}

        {/* C. Depth-Based Analysis (Explicit Fixed-Width Pressure Binning) */}
        <section className="bg-surface-0 border border-subtle rounded-[3px] p-4 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-subtle pb-3">
            <div>
              <div className="flex items-center gap-2 text-primary font-heading font-semibold text-base">
                <FiSliders className="text-accent" />
                <h2>Depth-Based BGC Analysis (Explicit Pressure Binning)</h2>
              </div>
              <p className="text-xs text-secondary font-mono mt-0.5">
                Paired strictly with <code className="text-primary">bgc_pressure</code> using fixed-width{" "}
                {binSize} dbar bins. No cross-grid interpolation.
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-2 text-xs font-mono">
              <select
                value={depthParam}
                onChange={(e) => setDepthParam(e.target.value)}
                aria-label="Depth analysis parameter"
                className="px-2.5 py-1.5 bg-surface-1 border border-strong rounded-[2px] text-primary"
              >
                {BGC_PARAMS.map((p) => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>

              <select
                value={binSize}
                onChange={(e) => setBinSize(Number(e.target.value))}
                aria-label="Pressure bin size"
                className="px-2.5 py-1.5 bg-surface-1 border border-strong rounded-[2px] text-primary"
              >
                {BIN_SIZES.map((b) => (
                  <option key={b} value={b}>{b} dbar bins</option>
                ))}
              </select>

              <input
                type="number"
                placeholder="Optional WMO"
                value={depthWmo}
                onChange={(e) => setDepthWmo(e.target.value)}
                className="w-32 px-2.5 py-1.5 bg-surface-1 border border-strong rounded-[2px] text-primary placeholder:text-muted/60"
              />
            </div>
          </div>

          {depthLoading ? (
            <Loading message="Binning BGC observations by pressure..." />
          ) : depthError ? (
            <ErrorState message={depthError} onRetry={loadDepthProfile} />
          ) : !depthData?.bins?.length ? (
            <EmptyState
              title="No depth-binned observations"
              message={`No valid ${depthParam} measurements with bgc_pressure found for the selected filter.`}
            />
          ) : (
            <div className="space-y-2">
              <div className="text-xs font-mono text-secondary flex flex-wrap gap-x-4 gap-y-1 bg-surface-1 border border-subtle rounded-[2px] px-3 py-2">
                <span>Method: <strong className="text-primary">{depthData.binning_method}</strong></span>
                <span>Pressure Axis: <strong className="text-primary">{depthData.pressure_axis}</strong></span>
                <span>Profiles Evaluated: <strong className="text-primary">{depthData.profiles_with_valid_data}</strong></span>
                <span>Bins: <strong className="text-primary">{depthData.bins.length}</strong></span>
              </div>

              <Plot
                data={[
                  {
                    x: depthData.bins.map((b) => b.mean),
                    y: depthData.bins.map((b) => b.bin_mid_dbar),
                    customdata: depthData.bins.map((b) => [
                      b.bin_start_dbar,
                      b.bin_end_dbar,
                      b.minimum,
                      b.maximum,
                      b.observation_count,
                      b.profile_count,
                    ]),
                    type: "scatter",
                    mode: "lines+markers",
                    name: `${depthData.parameter} Mean`,
                    line: { color: seriesColors[0], width: 2 },
                    marker: { color: seriesColors[0], size: 5 },
                    hovertemplate:
                      "<b>Bin</b>: %{customdata[0]}–%{customdata[1]} dbar<br>" +
                      `<b>Mean ${depthData.parameter}</b>: %{x} ${depthData.unit}<br>` +
                      "<b>Min / Max</b>: %{customdata[2]} / %{customdata[3]}<br>" +
                      "<b>Obs / Profiles</b>: %{customdata[4]} / %{customdata[5]}<extra></extra>",
                  },
                ]}
                layout={{
                  paper_bgcolor: "rgba(0,0,0,0)",
                  plot_bgcolor: plotBg,
                  font: { color: fontColor, size: 11, family: "IBM Plex Mono, monospace" },
                  margin: { l: 65, r: 20, t: 36, b: 48 },
                  title: {
                    text: `${depthData.parameter} Mean vs BGC Pressure (${depthData.bin_size_dbar} dbar bins)`,
                    font: { color: fontColor, size: 13, family: "Chivo, sans-serif" },
                  },
                  xaxis: {
                    gridcolor: gridColor,
                    title: { text: `${depthData.parameter} (${depthData.unit})`, standoff: 8 },
                  },
                  yaxis: {
                    gridcolor: gridColor,
                    title: { text: "BGC Pressure (dbar)", standoff: 8 },
                    autorange: "reversed",
                  },
                  hoverlabel: { bgcolor: hoverBg, font: { color: fontColor } },
                }}
                config={{ displayModeBar: false, responsive: true }}
                style={{ width: "100%", height: 360 }}
                useResizeHandler
              />
            </div>
          )}
        </section>

        {/* D. Profile Comparison */}
        <section className="bg-surface-0 border border-subtle rounded-[3px] p-4 space-y-4">
          <div className="border-b border-subtle pb-3">
            <div className="flex items-center gap-2 text-primary font-heading font-semibold text-base">
              <FiGitMerge className="text-accent" />
              <h2>Profile Comparison (Native Pressure Grids)</h2>
            </div>
            <p className="text-xs text-secondary font-mono mt-0.5">
              Compare 2 to 5 profiles by Profile ID. Each profile is plotted on its own pressure array; grids are never merged.
            </p>
          </div>

          <form onSubmit={handleCompareSubmit} className="flex flex-wrap items-end gap-3 text-xs font-mono">
            <div className="flex-1 min-w-[220px]">
              <label className="block text-secondary mb-1 uppercase text-[11px]">
                Profile IDs (2–5 comma-separated)
              </label>
              <input
                type="text"
                placeholder="e.g. 1386, 1387"
                value={compareIdsInput}
                onChange={(e) => setCompareIdsInput(e.target.value)}
                className="w-full px-3 py-1.5 bg-surface-1 border border-strong rounded-[2px] text-primary"
              />
            </div>

            <div>
              <label className="block text-secondary mb-1 uppercase text-[11px]">Parameter</label>
              <select
                value={compareParam}
                onChange={(e) => setCompareParam(e.target.value)}
                className="px-3 py-1.5 bg-surface-1 border border-strong rounded-[2px] text-primary"
              >
                {COMPARE_PARAMS.map((p) => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>
            </div>

            <button
              type="submit"
              className="px-4 py-1.5 bg-accent text-accent-contrast font-medium rounded-[2px] hover:opacity-90 cursor-pointer"
            >
              Compare Profiles
            </button>
          </form>

          {compareLoading ? (
            <Loading message="Loading profile series for comparison..." />
          ) : compareError ? (
            <ErrorState message={compareError} />
          ) : compareData ? (
            <div className="space-y-3">
              <p className="text-xs font-mono text-secondary">{compareData.grid_note}</p>

              {/* Unavailable profile notices */}
              {compareData.profiles
                .filter((p) => !p.available)
                .map((p) => (
                  <div
                    key={p.profile_id}
                    className="text-xs font-mono text-state-warning bg-surface-1 border border-state-warning/40 rounded-[2px] px-3 py-2"
                  >
                    Profile #{p.profile_id}: {p.reason}
                  </div>
                ))}

              {compareData.profiles.some((p) => p.available) ? (
                <Plot
                  data={compareData.profiles
                    .filter((p) => p.available)
                    .map((p, idx) => ({
                      x: p.values,
                      y: p.pressures,
                      type: "scatter",
                      mode: "lines+markers",
                      name: `Profile #${p.profile_id} (WMO ${p.wmo_number})`,
                      line: { color: seriesColors[idx % seriesColors.length], width: 1.8 },
                      marker: { color: seriesColors[idx % seriesColors.length], size: 4 },
                      hovertemplate:
                        `<b>Profile #${p.profile_id}</b><br>` +
                        `<b>${compareData.parameter}</b>: %{x} ${compareData.unit}<br>` +
                        `<b>${compareData.pressure_axis}</b>: %{y} dbar<extra></extra>`,
                    }))}
                  layout={{
                    paper_bgcolor: "rgba(0,0,0,0)",
                    plot_bgcolor: plotBg,
                    font: { color: fontColor, size: 11, family: "IBM Plex Mono, monospace" },
                    margin: { l: 65, r: 20, t: 36, b: 48 },
                    title: {
                      text: `${compareData.parameter} Comparison vs ${compareData.pressure_axis}`,
                      font: { color: fontColor, size: 13, family: "Chivo, sans-serif" },
                    },
                    xaxis: {
                      gridcolor: gridColor,
                      title: { text: `${compareData.parameter} (${compareData.unit})`, standoff: 8 },
                    },
                    yaxis: {
                      gridcolor: gridColor,
                      title: { text: `${compareData.pressure_axis} (dbar)`, standoff: 8 },
                      autorange: "reversed",
                    },
                    legend: { orientation: "h", y: -0.2 },
                    hoverlabel: { bgcolor: hoverBg, font: { color: fontColor } },
                  }}
                  config={{ displayModeBar: false, responsive: true }}
                  style={{ width: "100%", height: 380 }}
                  useResizeHandler
                />
              ) : (
                <EmptyState
                  title="No comparable data"
                  message={`None of the selected profiles contain valid ${compareData.parameter} observations.`}
                />
              )}
            </div>
          ) : null}
        </section>
        </div>
      </main>
    </div>
  );
}

const StatCard = ({ label, value, sub, accent = false }) => (
  <div className="bg-surface-0 border border-subtle rounded-[3px] p-4">
    <p className="text-[11px] uppercase tracking-wider text-muted font-mono">{label}</p>
    <p className={`text-2xl font-bold font-heading mt-1 ${accent ? "text-accent" : "text-primary"}`}>
      {value ?? "--"}
    </p>
    <p className="text-xs text-secondary mt-0.5">{sub}</p>
  </div>
);
