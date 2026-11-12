import React, { useState, useEffect, useRef, useCallback } from "react";
import Navbar from "../components/Navbar";
import Sidebar from "../components/Sidebar";
import SummaryCards from "../components/DataExplorer/SummaryCards";
import FilterPanel from "../components/DataExplorer/FilterPanel";
import FloatsTable from "../components/DataExplorer/FloatsTable";
import ProfilesTable from "../components/DataExplorer/ProfilesTable";
import ProfileDetailDrawer from "../components/DataExplorer/ProfileDetailDrawer";
import { getOverview, getFloats, getProfiles, extractErrorMessage } from "../api";
import { FiLayers, FiList } from "react-icons/fi";

const INITIAL_FILTERS = {
  wmo_number: "",
  profile_id: "",
  cycle_number: "",
  start_date: "",
  end_date: "",
  min_lat: "",
  max_lat: "",
  min_lon: "",
  max_lon: "",
};

const DataExplorer = () => {
  // Navigation / Tabs
  const [activeTab, setActiveTab] = useState("floats"); // "floats" | "profiles"

  // Profile detail drawer
  const [selectedProfileId, setSelectedProfileId] = useState(null);

  // Overview metrics state
  const [overview, setOverview] = useState(null);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [overviewError, setOverviewError] = useState(null);

  // Filters state
  const [appliedFilters, setAppliedFilters] = useState(INITIAL_FILTERS);

  // Floats Tab State
  const [floats, setFloats] = useState([]);
  const [floatsPagination, setFloatsPagination] = useState({ page: 1, limit: 20, total: 0, total_pages: 0 });
  const [floatsLoading, setFloatsLoading] = useState(false);
  const [floatsError, setFloatsError] = useState(null);
  const [floatsSortBy, setFloatsSortBy] = useState("wmo_number");
  const [floatsSortDir, setFloatsSortDir] = useState("asc");

  // Profiles Tab State
  const [profiles, setProfiles] = useState([]);
  const [profilesPagination, setProfilesPagination] = useState({ page: 1, limit: 20, total: 0, total_pages: 0 });
  const [profilesLoading, setProfilesLoading] = useState(false);
  const [profilesError, setProfilesError] = useState(null);
  const [profilesSortBy, setProfilesSortBy] = useState("profile_time");
  const [profilesSortDir, setProfilesSortDir] = useState("desc");

  // AbortController refs to cancel in-flight requests when parameters/filters change
  const floatsAbortRef = useRef(null);
  const profilesAbortRef = useRef(null);

  // 1. Fetch Overview (once on mount or retry)
  const loadOverview = useCallback(async () => {
    setOverviewLoading(true);
    setOverviewError(null);
    try {
      const data = await getOverview();
      setOverview(data);
    } catch (err) {
      const msg = extractErrorMessage(err);
      if (msg) setOverviewError(msg);
    } finally {
      setOverviewLoading(false);
    }
  }, []);

  useEffect(() => {
    loadOverview();
  }, [loadOverview]);

  // 2. Fetch Floats
  const loadFloats = useCallback(
    async (page = 1, sortBy = floatsSortBy, sortDir = floatsSortDir, search = appliedFilters.wmo_number) => {
      if (floatsAbortRef.current) {
        floatsAbortRef.current.abort();
      }
      floatsAbortRef.current = new AbortController();

      setFloatsLoading(true);
      setFloatsError(null);

      try {
        const params = {
          page,
          limit: 20,
          sort_by: sortBy,
          sort_dir: sortDir,
        };
        if (search) params.search = search;

        const data = await getFloats(params, floatsAbortRef.current.signal);
        setFloats(data.floats || []);
        setFloatsPagination(data.pagination || { page, limit: 20, total: 0, total_pages: 0 });
      } catch (err) {
        const msg = extractErrorMessage(err);
        if (msg) setFloatsError(msg);
      } finally {
        setFloatsLoading(false);
      }
    },
    [floatsSortBy, floatsSortDir, appliedFilters.wmo_number]
  );

  // 3. Fetch Profiles
  const loadProfiles = useCallback(
    async (page = 1, sortBy = profilesSortBy, sortDir = profilesSortDir, currentFilters = appliedFilters) => {
      if (profilesAbortRef.current) {
        profilesAbortRef.current.abort();
      }
      profilesAbortRef.current = new AbortController();

      setProfilesLoading(true);
      setProfilesError(null);

      try {
        const params = {
          page,
          limit: 20,
          sort_by: sortBy,
          sort_dir: sortDir,
        };

        // Attach non-empty filter parameters
        if (currentFilters.wmo_number) params.wmo_number = currentFilters.wmo_number;
        if (currentFilters.profile_id) params.profile_id = currentFilters.profile_id;
        if (currentFilters.cycle_number) params.cycle_number = currentFilters.cycle_number;
        if (currentFilters.start_date) params.start_date = currentFilters.start_date;
        if (currentFilters.end_date) params.end_date = currentFilters.end_date;
        if (currentFilters.min_lat) params.min_lat = currentFilters.min_lat;
        if (currentFilters.max_lat) params.max_lat = currentFilters.max_lat;
        if (currentFilters.min_lon) params.min_lon = currentFilters.min_lon;
        if (currentFilters.max_lon) params.max_lon = currentFilters.max_lon;

        const data = await getProfiles(params, profilesAbortRef.current.signal);
        setProfiles(data.profiles || []);
        setProfilesPagination(data.pagination || { page, limit: 20, total: 0, total_pages: 0 });
      } catch (err) {
        const msg = extractErrorMessage(err);
        if (msg) setProfilesError(msg);
      } finally {
        setProfilesLoading(false);
      }
    },
    [profilesSortBy, profilesSortDir, appliedFilters]
  );

  // Trigger data load when tab, filters, or sorting change
  useEffect(() => {
    if (activeTab === "floats") {
      loadFloats(floatsPagination.page, floatsSortBy, floatsSortDir, appliedFilters.wmo_number);
    } else {
      loadProfiles(profilesPagination.page, profilesSortBy, profilesSortDir, appliedFilters);
    }
  }, [activeTab]);

  // Handle Floats Sorting
  const handleFloatsSort = (newSortBy, newSortDir) => {
    setFloatsSortBy(newSortBy);
    setFloatsSortDir(newSortDir);
    loadFloats(1, newSortBy, newSortDir, appliedFilters.wmo_number);
  };

  // Handle Profiles Sorting
  const handleProfilesSort = (newSortBy, newSortDir) => {
    setProfilesSortBy(newSortBy);
    setProfilesSortDir(newSortDir);
    loadProfiles(1, newSortBy, newSortDir, appliedFilters);
  };

  // Filter Actions
  const handleApplyFilters = (newFilters) => {
    setAppliedFilters(newFilters);
    if (activeTab === "floats") {
      loadFloats(1, floatsSortBy, floatsSortDir, newFilters.wmo_number);
    } else {
      loadProfiles(1, profilesSortBy, profilesSortDir, newFilters);
    }
  };

  const handleClearFilters = () => {
    setAppliedFilters(INITIAL_FILTERS);
    if (activeTab === "floats") {
      loadFloats(1, floatsSortBy, floatsSortDir, "");
    } else {
      loadProfiles(1, profilesSortBy, profilesSortDir, INITIAL_FILTERS);
    }
  };

  // Switch from Floats tab to Profiles tab for a specific WMO
  const handleSelectWmo = (wmoNumber) => {
    const updated = { ...appliedFilters, wmo_number: String(wmoNumber) };
    setAppliedFilters(updated);
    setActiveTab("profiles");
    loadProfiles(1, profilesSortBy, profilesSortDir, updated);
  };

  return (
    <div className="bg-[#000000] min-h-screen text-white">
      <Navbar />
      <Sidebar />

      {/* Profile Detail Drawer */}
      <ProfileDetailDrawer
        profileId={selectedProfileId}
        onClose={() => setSelectedProfileId(null)}
      />

      {/* Main Content Area */}
      <main className="ml-60 pt-20 p-6 max-w-7xl">
        {/* Module Header */}
        <div className="mb-6">
          <div className="flex items-center gap-3 mb-2">
            <div className="p-2.5 rounded-xl bg-blue-600/20 border border-blue-500/30 text-blue-400 text-2xl">
              <FiLayers />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-white">Data Explorer</h1>
              <p className="text-sm text-[#BDC1CAFF]">
                Explore, filter, and inspect oceanographic profiles and float trajectories from the ARGO profiling network.
              </p>
            </div>
          </div>
        </div>

        {/* Global Summary Cards */}
        {overviewError ? (
          <div className="p-4 mb-6 rounded-xl bg-red-900/30 border border-red-500/50 text-red-200 text-sm flex items-center justify-between">
            <span>{overviewError}</span>
            <button
              onClick={loadOverview}
              className="px-3 py-1 bg-red-800 hover:bg-red-700 rounded text-xs text-white"
            >
              Retry
            </button>
          </div>
        ) : (
          <SummaryCards overview={overview} loading={overviewLoading} />
        )}

        {/* Filter Panel */}
        <FilterPanel
          filters={appliedFilters}
          onApply={handleApplyFilters}
          onClear={handleClearFilters}
          resultCount={activeTab === "floats" ? floatsPagination.total : profilesPagination.total}
          activeTab={activeTab}
        />

        {/* Navigation Tabs */}
        <div className="flex items-center gap-2 mb-4 border-b border-gray-800">
          <button
            onClick={() => setActiveTab("floats")}
            className={`flex items-center gap-2 px-5 py-2.5 text-sm font-medium border-b-2 transition ${
              activeTab === "floats"
                ? "border-blue-500 text-blue-400 bg-blue-950/20"
                : "border-transparent text-[#BDC1CAFF] hover:text-white hover:border-gray-700"
            }`}
          >
            <FiLayers className="text-base" />
            <span>Floats</span>
            {floatsPagination.total > 0 && (
              <span className="ml-1 text-xs px-2 py-0.5 rounded-full bg-gray-800 text-gray-300">
                {floatsPagination.total}
              </span>
            )}
          </button>

          <button
            onClick={() => setActiveTab("profiles")}
            className={`flex items-center gap-2 px-5 py-2.5 text-sm font-medium border-b-2 transition ${
              activeTab === "profiles"
                ? "border-blue-500 text-blue-400 bg-blue-950/20"
                : "border-transparent text-[#BDC1CAFF] hover:text-white hover:border-gray-700"
            }`}
          >
            <FiList className="text-base" />
            <span>Profiles</span>
            {profilesPagination.total > 0 && (
              <span className="ml-1 text-xs px-2 py-0.5 rounded-full bg-gray-800 text-gray-300">
                {profilesPagination.total.toLocaleString()}
              </span>
            )}
          </button>
        </div>

        {/* Tab Views */}
        {activeTab === "floats" ? (
          <FloatsTable
            floats={floats}
            pagination={floatsPagination}
            loading={floatsLoading}
            error={floatsError}
            sortBy={floatsSortBy}
            sortDir={floatsSortDir}
            onSort={handleFloatsSort}
            onPageChange={(newPage) => loadFloats(newPage, floatsSortBy, floatsSortDir, appliedFilters.wmo_number)}
            onSelectWmo={handleSelectWmo}
            projectsRecorded={overview?.projects?.recorded ?? false}
            onRetry={() => loadFloats(floatsPagination.page, floatsSortBy, floatsSortDir, appliedFilters.wmo_number)}
          />
        ) : (
          <ProfilesTable
            profiles={profiles}
            pagination={profilesPagination}
            loading={profilesLoading}
            error={profilesError}
            sortBy={profilesSortBy}
            sortDir={profilesSortDir}
            onSort={handleProfilesSort}
            onPageChange={(newPage) => loadProfiles(newPage, profilesSortBy, profilesSortDir, appliedFilters)}
            onClearFilters={handleClearFilters}
            onRetry={() => loadProfiles(profilesPagination.page, profilesSortBy, profilesSortDir, appliedFilters)}
            onSelectProfile={(id) => setSelectedProfileId(id)}
          />
        )}

        {/* Footnote */}
        <footer className="mt-8 pt-4 border-t border-gray-800/80 text-center">
          <p className="text-xs text-[#BDC1CAFF]">
            Raw, unadjusted measurements. No QC flags in the current database.
          </p>
        </footer>
      </main>
    </div>
  );
};

export default DataExplorer;
