import axios from "axios";

export const BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:5000";

const apiClient = axios.create({
  baseURL: BASE_URL,
  timeout: 15000,
});

/**
 * Extracts a user-facing error message from an axios error.
 */
export const extractErrorMessage = (error) => {
  if (axios.isCancel(error)) {
    return null; // Request was aborted, not an error to show
  }
  if (error.response) {
    // The server responded with a non-2xx status code
    return error.response.data?.error || `Server responded with status ${error.response.status}`;
  }
  if (error.request) {
    // The request was made but no response was received
    return "Cannot reach backend server. Please verify the Flask backend is running on http://localhost:5000.";
  }
  return error.message || "An unknown error occurred.";
};

/**
 * Fetch dataset-level overview metrics.
 */
export const getOverview = async (signal) => {
  const res = await apiClient.get("/api/data/overview", { signal });
  return res.data;
};

/**
 * Fetch paginated floats list with optional search and sorting.
 */
export const getFloats = async (params = {}, signal) => {
  const res = await apiClient.get("/api/data/floats", { params, signal });
  return res.data;
};

/**
 * Fetch single float metadata and profile summary by WMO.
 */
export const getFloatByWmo = async (wmoNumber, signal) => {
  const res = await apiClient.get(`/api/data/floats/${wmoNumber}`, { signal });
  return res.data;
};

/**
 * Fetch paginated profiles list with filters and sorting.
 */
export const getProfiles = async (params = {}, signal) => {
  const res = await apiClient.get("/api/data/profiles", { params, signal });
  return res.data;
};

/**
 * Fetch profile detail including depth measurement arrays.
 */
export const getProfileDetail = async (profileId, signal) => {
  const res = await apiClient.get(`/api/data/profiles/${profileId}`, { signal });
  return res.data;
};

/**
 * Fetch lightweight map points for spatial visualization.
 */
export const getMapPoints = async (params = {}, signal) => {
  const res = await apiClient.get("/api/data/map-points", { params, signal });
  return res.data;
};

export default apiClient;
