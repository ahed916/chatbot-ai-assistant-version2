// api.ts
import axios from "axios";
import { supabase } from "../lib/supabase";

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL,
});

api.interceptors.request.use(async (config) => {
  const { data: { session } } = await supabase.auth.getSession();
  if (session?.access_token) {
    config.headers.Authorization = `Bearer ${session.access_token}`;
  }
  return config;
});

api.interceptors.response.use(
  (res) => res,
  async (error) => {
    const status = error.response?.status;
    const detail = error.response?.data?.detail ?? "";

    if (status === 401) {
      // Try to refresh the session before giving up
      const { data: { session }, error: refreshError } = await supabase.auth.refreshSession();

      if (!refreshError && session?.access_token) {
        // Retry the original request with the new token
        const originalRequest = error.config;
        originalRequest.headers.Authorization = `Bearer ${session.access_token}`;
        return api(originalRequest);
      }

      // Refresh failed — now it's safe to sign out and redirect
      await supabase.auth.signOut();
      window.location.href = "/login";
    }

    // Redmine key invalid — force reconnection
    if (status === 400 && detail.includes("Redmine API key not configured")) {
      window.location.href = "/setup-redmine-key";
    }

    return Promise.reject(error);
  }
);