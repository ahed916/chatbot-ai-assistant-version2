/**
 * useChatState.ts
 *
 * All types imported from @/types/chat — never redefined here.
 *
 * Risk alert deduplication:
 *   Deduplication is handled purely via React state (checked_at field).
 *   sessionStorage is intentionally NOT used for alert suppression —
 *   it permanently blocked alerts after the first poll, even across page reloads.
 *
 * Session ID:
 *   Tied to the authenticated Supabase user ID (session.user.id).
 *   This ensures the same session ID is sent on every request, including
 *   polling, so the backend can resolve history correctly.
 */
import { useState, useCallback, useRef, useEffect } from "react";
import type { Conversation, RiskAlert, DashboardPayload, ChatMessage } from "../types/chat";
import { supabase } from "../lib/supabase";
import { api } from "../api/client";


// ── Constants ─────────────────────────────────────────────────────────────────

const STREAM_URL    = "http://localhost:8000/chat/stream";
const CHAT_URL      = "http://localhost:8000/chat";
const PROACTIVE_URL = "http://localhost:8000/api/proactive-risks";

const createId = () => Math.random().toString(36).slice(2, 10);

// ── Dashboard detection ───────────────────────────────────────────────────────

function extractDashboard(content: string): {
  isDashboard: boolean;
  payload?: DashboardPayload;
  cleanContent?: string;
} {
  const trimmed = content.trim();

  const DASHBOARD_TYPES = new Set([
    "dashboard", "quick_stat", "clarification", "no_data",
  ]);

  // 1. Pure JSON
  if (trimmed.startsWith("{")) {
    try {
      const parsed = JSON.parse(trimmed) as DashboardPayload;
      if (DASHBOARD_TYPES.has(parsed.type)) {
        return { isDashboard: true, payload: parsed, cleanContent: trimmed };
      }
    } catch {}
  }

  // 2. JSON in ```json ... ``` fences
  const fenceMatch = trimmed.match(/```(?:json)?\s*(\{[\s\S]*?\})\s*```/);
  if (fenceMatch) {
    try {
      const parsed = JSON.parse(fenceMatch[1]) as DashboardPayload;
      if (DASHBOARD_TYPES.has(parsed.type)) {
        return { isDashboard: true, payload: parsed, cleanContent: fenceMatch[1] };
      }
    } catch {}
  }

  // 3. JSON embedded in text
  for (const marker of [
    '{"type":"dashboard"',     '{"type": "dashboard"',
    '{"type":"quick_stat"',    '{"type": "quick_stat"',
    '{"type":"clarification"', '{"type": "clarification"',
    '{"type":"no_data"',       '{"type": "no_data"',
  ]) {
    const idx = trimmed.indexOf(marker);
    if (idx !== -1) {
      try {
        const parsed = JSON.parse(trimmed.slice(idx)) as DashboardPayload;
        if (DASHBOARD_TYPES.has(parsed.type)) {
          return { isDashboard: true, payload: parsed, cleanContent: trimmed.slice(idx) };
        }
      } catch {}
    }
  }

  return { isDashboard: false };
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface ProactiveRiskResponse {
  has_alert:       boolean;
  message:         string | null;
  critical_count:  number;
  slack_sent:      boolean;
  overall_health:  string;
  recommendations: string[];
  risks?:          unknown[];
  checked_at?:     string | null;
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useChatState() {
  const [conversations,     setConversations]     = useState<Conversation[]>([]);
  const [activeId,          setActiveId]          = useState<string | null>(null);
  const [isTyping,          setIsTyping]          = useState(false);
  const [isLoadingHistory,  setIsLoadingHistory]  = useState(false);
  const [riskAlerts,        setRiskAlerts]        = useState<RiskAlert[]>([]);

  // Session ID is derived from the authenticated Supabase user — set lazily
  // by getAuthHeaders() on the first request.
  const sessionIdRef = useRef<string>("");

  const conversationsRef = useRef(conversations);
  const riskAlertsRef    = useRef(riskAlerts);

  useEffect(() => { conversationsRef.current = conversations; }, [conversations]);
  useEffect(() => { riskAlertsRef.current    = riskAlerts;    }, [riskAlerts]);

  const activeConversation =
    conversations.find((c) => c.id === activeId) ?? null;

  // ── Auth headers helper ───────────────────────────────────────────────────

  const getAuthHeaders = useCallback(async (): Promise<Record<string, string>> => {
    const { data: { session } } = await supabase.auth.getSession();
    if (!session?.access_token) throw new Error("Not authenticated");
    sessionIdRef.current = session.user.id;
    return {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${session.access_token}`,
      "X-Session-Id": session.user.id,
    };
  }, []);

  // ── Load conversations from DB on mount ───────────────────────────────────

  useEffect(() => {
    const loadHistory = async () => {
      setIsLoadingHistory(true);
      try {
        const res = await api.get("/conversations");
        const dbConversations: Conversation[] = res.data.map((c: any) => ({
          id:        c.session_id || c.id,
          dbId:      c.id,
          title:     c.title || "Chat",
          messages:  [],
          createdAt: new Date(c.created_at),
          updatedAt: new Date(c.updated_at),
        }));
        if (dbConversations.length > 0) {
          setConversations(dbConversations);
          setActiveId(dbConversations[0].id);
        }
      } catch (e) {
        console.warn("[History] Failed to load conversations:", e);
      } finally {
        setIsLoadingHistory(false);
      }
    };
    loadHistory();
  }, []);

  // ── Proactive risk polling ────────────────────────────────────────────────
  // Deduplication uses React state only (checked_at field).
  // sessionStorage suppression was removed — it permanently blocked alerts
  // after the first poll, even across page reloads.

  useEffect(() => {
    const checkProactiveRisks = async () => {
      try {
        const { data: { session } } = await supabase.auth.getSession();
        if (!session?.access_token) return;

        const res = await fetch(PROACTIVE_URL, {
          headers: {
            "Authorization": `Bearer ${session.access_token}`,
            "X-Session-Id": session.user.id,
          },
        });
        if (!res.ok) return;
        const data: ProactiveRiskResponse = await res.json();

        if (!data.has_alert || !data.message) return;

        const alertKey = data.checked_at ?? null;
        if (!alertKey) return;

        // Deduplicate using React state only — no sessionStorage.
        const inState = riskAlertsRef.current.some((a) => a.checked_at === alertKey);
        if (inState) return;

        const alert: RiskAlert = {
          id:              createId(),
          message:         data.message,
          critical_count:  data.critical_count,
          overall_health:  data.overall_health,
          recommendations: data.recommendations ?? [],
          risks:           data.risks ?? [],
          checked_at:      alertKey,
          read:            false,
        };

        setRiskAlerts((prev) => [alert, ...prev]);

      } catch (e) {
        console.warn("[Proactive] Risk check failed:", e);
      }
    };

    // Fire immediately on mount — bell lights up on first load
    checkProactiveRisks();
    const interval = setInterval(checkProactiveRisks, 5 * 60 * 1000);
    return () => clearInterval(interval);
  }, []);

  // ── Conversation management ───────────────────────────────────────────────

  const createConversation = useCallback((): string => {
    const current = conversationsRef.current.find((c) => c.id === activeId);
    if (current && current.messages.length === 0 && !current.isProactive) {
      return current.id;
    }
    const conv: Conversation = {
      id: createId(), title: "New Chat",
      messages: [], createdAt: new Date(), updatedAt: new Date(),
    };
    setConversations((prev) => [conv, ...prev]);
    setActiveId(conv.id);
    return conv.id;
  }, [activeId]);

  const selectConversation = useCallback(async (id: string) => {
    setActiveId(id);
    const conv = conversationsRef.current.find((c) => c.id === id);
    if (conv && conv.messages.length === 0 && conv.dbId) {
      try {
        const res = await api.get(`/conversations/${conv.dbId}/messages`);
        const messages: ChatMessage[] = res.data.map((m: any) => {
          // Run dashboard detection on every assistant message loaded from DB
          if (m.role === "assistant") {
            const { isDashboard, payload, cleanContent } = extractDashboard(m.content);
            return {
              id:          createId(),
              role:        "assistant" as const,
              content:     cleanContent ?? m.content,
              timestamp:   new Date(m.created_at),
              isDashboard,
              dashboard:   payload,
            };
          }
          return {
            id:        createId(),
            role:      m.role as "user" | "assistant",
            content:   m.content,
            timestamp: new Date(m.created_at),
          };
        });
        setConversations((prev) =>
          prev.map((c) => (c.id === id ? { ...c, messages } : c))
        );
      } catch (e) {
        console.warn("[History] Failed to load messages:", e);
      }
    }
  }, []);

  const deleteConversation = useCallback(async (id: string) => {
    const conv = conversationsRef.current.find((c) => c.id === id);
    // Optimistic update
    setConversations((prev) => prev.filter((c) => c.id !== id));
    setActiveId((prev) => {
      if (prev !== id) return prev;
      const remaining = conversationsRef.current.filter((c) => c.id !== id);
      return remaining[0]?.id ?? null;
    });
    // Delete from DB if it has a DB record
    if (conv?.dbId) {
      try {
        await api.delete(`/conversations/${conv.dbId}`);
      } catch (e) {
        console.warn("[History] Failed to delete conversation:", e);
      }
    }
  }, []);

  const updateTitle = useCallback((convId: string, content: string) => {
    const title = content.length > 40 ? content.slice(0, 40) + "…" : content;
    setConversations((prev) =>
      prev.map((c) => (c.id === convId ? { ...c, title } : c))
    );
  }, []);

  // ── Logout ────────────────────────────────────────────────────────────────

  const logout = useCallback(async () => {
    await supabase.auth.signOut();
    window.location.href = "/login";
  }, []);

  // ── Risk alert actions ────────────────────────────────────────────────────

  const openRiskAlert = useCallback((alertId: string) => {
    const alert = riskAlertsRef.current.find((a) => a.id === alertId);
    if (!alert) return;

    if (alert.conversationId) {
      setActiveId(alert.conversationId);
      return;
    }

    let msgContent = alert.message;
    if (alert.recommendations?.length) {
      msgContent +=
        "\n\n**Recommended actions:**\n" +
        alert.recommendations.map((r, i) => `${i + 1}. ${r}`).join("\n");
    }

    const convId = createId();
    const conv: Conversation = {
      id: convId,
      title: `🚨 Risk Alert — ${alert.overall_health}`,
      isProactive: true,
      messages: [{
        id: createId(), role: "assistant",
        content: msgContent, timestamp: new Date(), isProactive: true,
      }],
      createdAt: new Date(), updatedAt: new Date(),
    };

    setConversations((prev) => [conv, ...prev]);
    setActiveId(convId);
    setRiskAlerts((prev) =>
      prev.map((a) =>
        a.id === alertId ? { ...a, read: true, conversationId: convId } : a
      )
    );
  }, []);

  const markAlertRead = useCallback((alertId: string) => {
    setRiskAlerts((prev) =>
      prev.map((a) => (a.id === alertId ? { ...a, read: true } : a))
    );
  }, []);

  // ── appendBotMessage ──────────────────────────────────────────────────────

  const appendBotMessage = useCallback((
    convId:     string,
    botMsgId:   string,
    content:    string,
    latencyMs?: number,
    final = true,
  ) => {
    let isDash   = false;
    let payload: DashboardPayload | undefined;
    let cleanContent = content;

    if (final) {
      const result = extractDashboard(content);
      isDash       = result.isDashboard;
      payload      = result.payload;
      cleanContent = result.cleanContent ?? content;
    }

    setConversations((prev) =>
      prev.map((conv) => {
        if (conv.id !== convId) return conv;
        const exists = conv.messages.some((m) => m.id === botMsgId);
        const finalMsg: ChatMessage = {
          id: botMsgId, role: "assistant",
          content: cleanContent, timestamp: new Date(),
          isDashboard: isDash, dashboard: payload, latencyMs,
        };
        return {
          ...conv,
          updatedAt: new Date(),
          messages: exists
            ? conv.messages.map((m) =>
                m.id === botMsgId
                  ? { ...m, content: cleanContent, isDashboard: isDash, dashboard: payload, latencyMs }
                  : m
              )
            : [...conv.messages, finalMsg],
        };
      })
    );
  }, []);

  // ── Send Message ──────────────────────────────────────────────────────────

  const sendMessage = useCallback(
    async (content: string) => {
      if (!content.trim()) return;

      const currentId = activeId ?? createConversation();
      const userMsg: ChatMessage = {
        id: createId(), role: "user", content, timestamp: new Date(),
      };

      const currentConv = conversationsRef.current.find((c) => c.id === currentId);
      const isFirst     = (currentConv?.messages ?? []).length === 0;

      const historyMessages = (currentConv?.messages ?? []).map((m) => ({
        role: m.role, content: m.content,
      }));
      const fullMessages = [...historyMessages, { role: "user", content }];

      setConversations((prev) =>
        prev.map((conv) =>
          conv.id === currentId
            ? { ...conv, messages: [...conv.messages, userMsg], updatedAt: new Date() }
            : conv
        )
      );

      if (isFirst && !currentConv?.isProactive) updateTitle(currentId, content);

      setIsTyping(true);
      const botMsgId      = createId();
      const requestStart  = Date.now();
      let streamSucceeded = false;

      // ── Streaming ─────────────────────────────────────────────────────────
      try {
        const authHeaders = await getAuthHeaders();
        const res = await fetch(STREAM_URL, {
          method:  "POST",
          headers: authHeaders,
          body:    JSON.stringify({ messages: fullMessages, conversation_id: currentId }),
        });
        if (!res.ok || !res.body) throw new Error("Stream unavailable");

        const reader  = res.body.getReader();
        const decoder = new TextDecoder();
        let fullReply   = "";
        let bubbleAdded = false;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          const raw   = decoder.decode(value, { stream: true });
          const lines = raw.split("\n").filter((l) => l.startsWith("data: "));

          for (const line of lines) {
            const data = line.slice(6).trim();
            if (data === "[DONE]") break;

            try {
              const parsed = JSON.parse(data);
              if (parsed.error) throw new Error(parsed.error);

              if (parsed.token) {
                streamSucceeded = true;
                fullReply += parsed.token;

                if (!bubbleAdded) {
                  setConversations((prev) =>
                    prev.map((conv) =>
                      conv.id !== currentId ? conv : {
                        ...conv,
                        messages: [
                          ...conv.messages,
                          {
                            id: botMsgId, role: "assistant" as const,
                            content: fullReply, timestamp: new Date(), isDashboard: false,
                          },
                        ],
                      }
                    )
                  );
                  setIsTyping(false);
                  bubbleAdded = true;
                } else {
                  setConversations((prev) =>
                    prev.map((conv) =>
                      conv.id !== currentId ? conv : {
                        ...conv,
                        messages: conv.messages.map((m) =>
                          m.id === botMsgId ? { ...m, content: fullReply } : m
                        ),
                      }
                    )
                  );
                }
              }
            } catch {
              // Malformed chunk — skip
            }
          }
        }

        if (streamSucceeded) {
          const latencyMs = Date.now() - requestStart;
          const { isDashboard, payload, cleanContent } = extractDashboard(fullReply);
          setConversations((prev) =>
            prev.map((conv) => {
              if (conv.id !== currentId) return conv;
              return {
                ...conv,
                updatedAt: new Date(),
                messages: conv.messages.map((m) =>
                  m.id === botMsgId
                    ? { ...m,
                        content: cleanContent ?? fullReply,
                        isDashboard,
                        dashboard: payload,
                        latencyMs,
                      }
                    : m
                ),
              };
            })
          );
        }

      } catch (streamErr) {
        console.warn("[Stream] Failed, falling back to /chat:", streamErr);
        setIsTyping(false);
      }

      // ── Fallback ──────────────────────────────────────────────────────────
      if (!streamSucceeded) {
        setIsTyping(true);
        try {
          const authHeaders = await getAuthHeaders();
          const res = await fetch(CHAT_URL, {
            method:  "POST",
            headers: authHeaders,
            body:    JSON.stringify({ messages: fullMessages, conversation_id: currentId }),
          });
          if (!res.ok) throw new Error(await res.text());
          const data = await res.json();
          appendBotMessage(currentId, botMsgId, data.reply, data.latency_ms, true);
        } catch (err) {
          console.error("[Chat] Fallback failed:", err);
          appendBotMessage(
            currentId, botMsgId,
            "❌ Sorry, I encountered an error. Please try again.",
            undefined, true,
          );
        } finally {
          setIsTyping(false);
        }
      }
    },
    [activeId, createConversation, updateTitle, appendBotMessage, getAuthHeaders],
  );

  return {
    conversations,
    activeConversation,
    activeId,
    isTyping,
    isLoadingHistory,
    riskAlerts,
    createConversation,
    selectConversation,
    deleteConversation,
    sendMessage,
    openRiskAlert,
    markAlertRead,
    logout,
  };
}