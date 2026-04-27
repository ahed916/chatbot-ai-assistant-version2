/**
 * Admin API client — mirrors the FastAPI /admin router.
 * Token is fetched from Supabase automatically on every call.
 * No mock data — all endpoints hit the real backend.
 */

import { supabase } from "./supabase";

const API_URL = (import.meta as any).env?.VITE_API_URL as string | undefined ?? "http://localhost:8000";

export interface PMUser {
  id: string;
  email: string;
  full_name: string;
  role: "project_manager";
  redmine_user_id: number | null;
  is_redmine_connected: boolean;
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string;
  session_id: string;
  created_at: string;
  updated_at: string;
}

export interface ConversationMessage {
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface RedmineStats {
  total_issues: number;
  total_projects: number;
  overdue: number;
  by_status: Record<string, number>;
  by_tracker: Record<string, number>;
  by_project: Record<string, number>;
}

export interface ChatActivityPoint {
  date: string;          // "2025-04-21"
  day: string;           // "Mon"
  conversations: number;
}

// ─── Token helper ─────────────────────────────────────────────────────────

async function getToken(): Promise<string> {
  const { data: { session } } = await supabase.auth.getSession();
  if (!session?.access_token) throw new Error("Not authenticated");
  return session.access_token;
}

// ─── Fetch helper ─────────────────────────────────────────────────────────

async function authedFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const token = await getToken();
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    let detail = text;
    try { detail = JSON.parse(text)?.detail ?? text; } catch {}
    throw new Error(detail || `Request failed: ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ─── Public API ───────────────────────────────────────────────────────────

export const adminApi = {
  listUsers(): Promise<PMUser[]> {
    return authedFetch<PMUser[]>("/admin/users");
  },

  createUser(body: { email: string; password: string; full_name: string }): Promise<{ id: string; email: string }> {
    return authedFetch("/admin/users", { method: "POST", body: JSON.stringify(body) });
  },

  deleteUser(userId: string): Promise<void> {
    return authedFetch<void>(`/admin/users/${userId}`, { method: "DELETE" });
  },

  listConversations(userId: string): Promise<Conversation[]> {
    return authedFetch(`/admin/users/${userId}/conversations`);
  },

  getMessages(conversationId: string): Promise<ConversationMessage[]> {
    return authedFetch(`/admin/conversations/${conversationId}/messages`);
  },

  getRedmineStats(): Promise<RedmineStats> {
    return authedFetch("/admin/redmine-stats");
  },

  /** Real conversation counts per day for the last 7 days — from Supabase via backend */
  getChatActivity(): Promise<ChatActivityPoint[]> {
    return authedFetch("/admin/chat-activity");
  },
};