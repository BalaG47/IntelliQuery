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
  const [floatsLoading, setFloatsLoading] = useState(true);
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

  // Click on "View Profiles" in a float row: switches tab to profiles & sets WMO filter
  const handleSelectWmo = (wmo) => {
    const updated = { ...appliedFilters, wmo_number: String(wmo) };
    setAppliedFilters(updated);
    setActiveTab("profiles");
    loadProfiles(1, profilesSortBy, profilesSortDir, updated);
  };

  return (
    <div className="bg-app min-h-screen text-primary">
      <Navbar />
      <Sidebar />

      {/* Profile Detail Drawer */}
      <ProfileDetailDrawer
        profileId={selectedProfileId}
        onClose={() => setSelectedProfileId(null)}
      />

      {/* Main Content Area */}
      <main className="lg:ml-60 ml-0 pt-20 p-4 sm:p-6 max-w-7xl min-w-0">
        {/* Module Header */}
        <div className="mb-6">
          <div className="flex items-center gap-3 mb-2">
            <div className="p-2.5 rounded-[2px] bg-surface-1 border border-subtle text-accent text-2xl shrink-0">
              <FiLayers />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-primary font-heading">
                Data Explorer
              </h1>
              <p className="text-sm text-secondary font-mono">
                Explore, filter, and inspect oceanographic profiles and float trajectories from the ARGO profiling network.
              </p>
            </div>
          </div>
        </div>

        {/* Global Summary Cards */}
        {overviewError ? (
          <div className="p-3.5 mb-6 rounded-[2px] bg-surface-1 border border-state-error/40 text-state-error text-xs font-mono flex items-center justify-between">
            <span>{overviewError}</span>
            <button
              type="button"
              onClick={loadOverview}
              className="px-3 py-1 bg-state-error hover:opacity-90 text-white dark:text-[#071A15] rounded-[2px] text-xs font-mono font-medium transition-colors cursor-pointer"
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
        <div className="flex items-center gap-2 mb-4 border-b border-subtle overflow-x-auto">
          <button
            type="button"
            onClick={() => setActiveTab("floats")}
            className={`flex items-center gap-2 px-4 py-2 text-xs font-mono uppercase tracking-wider border-b-2 transition-colors cursor-pointer ${
              activeTab === "floats"
                ? "border-accent text-accent font-semibold bg-surface-1/50"
                : "border-transparent text-secondary hover:text-primary hover:bg-surface-1/30"
            }`}
          >
            <FiLayers className="text-sm" />
            <span>Floats</span>
            {floatsPagination.total > 0 && (
              <span className="ml-1 text-[11px] px-1.5 py-0.5 rounded-[2px] bg-surface-1 border border-subtle text-muted">
                {floatsPagination.total}
              </span>
            )}
          </button>

          <button
            type="button"
            onClick={() => setActiveTab("profiles")}
            className={`flex items-center gap-2 px-4 py-2 text-xs font-mono uppercase tracking-wider border-b-2 transition-colors cursor-pointer ${
              activeTab === "profiles"
                ? "border-accent text-accent font-semibold bg-surface-1/50"
                : "border-transparent text-secondary hover:text-primary hover:bg-surface-1/30"
            }`}
          >
            <FiList className="text-sm" />
            <span>Profiles</span>
            {profilesPagination.total > 0 && (
              <span className="ml-1 text-[11px] px-1.5 py-0.5 rounded-[2px] bg-surface-1 border border-subtle text-muted">
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
            projectsRecorded={overview ? overview.projects?.recorded : undefined}
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
        <footer className="mt-8 pt-4 border-t border-subtle text-center">
          <p className="text-xs text-muted font-mono">
            Raw, unadjusted measurements. No QC flags in the current database.
          </p>
        </footer>
      </main>
    </div>
  );
};

export default DataExplorer;
