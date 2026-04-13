/**
 * useChatState.ts
 *
 * All types imported from @/types/chat — never redefined here.
 *
 * Risk alert deduplication:
 *   React state resets on every page reload, so seen alert IDs are also
 *   persisted in sessionStorage. This prevents the same alert from appearing
 *   multiple times within the same browser session.
 */
 import { useState, useCallback, useRef, useEffect } from "react";
 import {
   ChatMessage,
   Conversation,
   RiskAlert,
   DashboardPayload,
 } from "@/types/chat";
 
 // ── Constants ─────────────────────────────────────────────────────────────────
 
 const STREAM_URL    = "http://localhost:8000/chat/stream";
 const CHAT_URL      = "http://localhost:8000/chat";
 const PROACTIVE_URL = "http://localhost:8000/api/proactive-risks";
 
 // sessionStorage key for persisting seen alert IDs across polling cycles
 const SEEN_ALERTS_KEY = "redmind:seen_alert_ids";
 
 const createId = () => Math.random().toString(36).slice(2, 10);
 
 // ── Seen alerts persistence ───────────────────────────────────────────────────
 
 function getSeenAlertIds(): Set<string> {
   try {
     const raw = sessionStorage.getItem(SEEN_ALERTS_KEY);
     return raw ? new Set(JSON.parse(raw)) : new Set();
   } catch {
     return new Set();
   }
 }
 
 function addSeenAlertId(id: string): void {
   try {
     const ids = getSeenAlertIds();
     ids.add(id);
     // Keep only last 50 to prevent unbounded growth
     const arr = Array.from(ids).slice(-50);
     sessionStorage.setItem(SEEN_ALERTS_KEY, JSON.stringify(arr));
   } catch {
     // sessionStorage unavailable — degrade gracefully
   }
 }
 
 function hasSeenAlert(id: string): boolean {
   return getSeenAlertIds().has(id);
 }
 
 // ── Dashboard detection ───────────────────────────────────────────────────────
 
 function extractDashboard(content: string): {
  isDashboard: boolean;
  payload?: DashboardPayload;
  cleanContent?: string;
} {
  const trimmed = content.trim();

  const DASHBOARD_TYPES = new Set([
    "dashboard", "quick_stat", "clarification", "no_data"  // ← add these two
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

  // 3. JSON embedded in text — add clarification/no_data markers
  for (const marker of [
    '{"type":"dashboard"',    '{"type": "dashboard"',
    '{"type":"quick_stat"',   '{"type": "quick_stat"',
    '{"type":"clarification"','{"type": "clarification"',  // ← add
    '{"type":"no_data"',      '{"type": "no_data"',        // ← add
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
   const [conversations, setConversations] = useState<Conversation[]>([]);
   const [activeId,      setActiveId]      = useState<string | null>(null);
   const [isTyping,      setIsTyping]      = useState(false);
   const [riskAlerts,    setRiskAlerts]    = useState<RiskAlert[]>([]);

   const sessionIdRef = useRef<string>("");
 
   const conversationsRef = useRef(conversations);
   const riskAlertsRef    = useRef(riskAlerts);
 
   useEffect(() => { conversationsRef.current = conversations; }, [conversations]);
   useEffect(() => { riskAlertsRef.current    = riskAlerts;    }, [riskAlerts]);
 
   const activeConversation =
     conversations.find((c) => c.id === activeId) ?? null;
 
   // ── Proactive risk polling ────────────────────────────────────────────────
   useEffect(() => {
     const checkProactiveRisks = async () => {
       try {
        const res = await fetch(PROACTIVE_URL, {
          headers: {
            "X-Session-Id": sessionIdRef.current,
          },
        });
         if (!res.ok) return;
         const data: ProactiveRiskResponse = await res.json();
 
         if (!data.has_alert || !data.message) return;
 
         // The deduplication key: checked_at from backend (stable hash of risk state)
         const alertKey = data.checked_at ?? null;
 
         if (!alertKey) return; // No key = can't deduplicate = skip
 
         // Check 1: already in React state (within this render cycle)
         const inState = riskAlertsRef.current.some((a) => a.checked_at === alertKey);
         if (inState) return;
 
         // Check 2: already seen in this browser session (survives polling interval)
         if (hasSeenAlert(alertKey)) return;
 
         // New alert — add to state and mark as seen in sessionStorage
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
 
         addSeenAlertId(alertKey);
         setRiskAlerts((prev) => [alert, ...prev]);
 
       } catch (e) {
         console.warn("[Proactive] Risk check failed:", e);
       }
     };
 
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
 
   const selectConversation = useCallback((id: string) => setActiveId(id), []);
 
   const deleteConversation = useCallback((id: string) => {
     setConversations((prev) => prev.filter((c) => c.id !== id));
     setActiveId((prev) => {
       if (prev !== id) return prev;
       const remaining = conversationsRef.current.filter((c) => c.id !== id);
       return remaining[0]?.id ?? null;
     });
   }, []);
 
   const updateTitle = useCallback((convId: string, content: string) => {
     const title = content.length > 40 ? content.slice(0, 40) + "…" : content;
     setConversations((prev) =>
       prev.map((c) => (c.id === convId ? { ...c, title } : c))
     );
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
         const res = await fetch(STREAM_URL, {
           method: "POST",
           headers: {
            "Content-Type": "application/json",
            "X-Session-Id": sessionIdRef.current,
          },
           body: JSON.stringify({ messages: fullMessages }),
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
                           { id: botMsgId, role: "assistant" as const,
                             content: fullReply, timestamp: new Date(), isDashboard: false },
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
           appendBotMessage(currentId, botMsgId, fullReply, Date.now() - requestStart, true);
         }
 
       } catch (streamErr) {
         console.warn("[Stream] Failed, falling back to /chat:", streamErr);
         setIsTyping(false);
       }
 
       // ── Fallback ──────────────────────────────────────────────────────────
       if (!streamSucceeded) {
         setIsTyping(true);
         try {
           const res = await fetch(CHAT_URL, {
             method: "POST",
             headers: {
              "Content-Type": "application/json",
              "X-Session-Id": sessionIdRef.current,
            },
             body: JSON.stringify({ messages: fullMessages }),
           });
           if (!res.ok) throw new Error(await res.text());
           const data = await res.json();
           // 🔍 DEBUG LOGS — REMOVE AFTER DEMO
          console.log("🔍 [RISK POLL] ==============");
          console.log("has_alert:", data.has_alert);
          console.log("message:", data.message);
          console.log("checked_at:", data.checked_at);
          console.log("overall_health:", data.overall_health);

          const alertKey = data.checked_at ?? null;
          const inState = riskAlertsRef.current.some((a) => a.checked_at === alertKey);
          const inSession = hasSeenAlert(alertKey);

          console.log("alertKey:", alertKey);
          console.log("inState:", inState);
          console.log("inSession:", inSession);
          console.log("Will create alert?", !inState && !inSession && data.has_alert && data.message);
          console.log("🔍 [RISK POLL] ==============\n");
          // END DEBUG
           appendBotMessage(currentId, botMsgId, data.reply, data.latency_ms, true);
         } catch (err) {
           console.error("[Chat] Fallback failed:", err);
           appendBotMessage(currentId, botMsgId,
             "❌ Sorry, I encountered an error. Please try again.", undefined, true);
         } finally {
           setIsTyping(false);
         }
       }
     },
     [activeId, createConversation, updateTitle, appendBotMessage]
   );
 
   return {
     conversations,
     activeConversation,
     activeId,
     isTyping,
     riskAlerts,
     createConversation,
     selectConversation,
     deleteConversation,
     sendMessage,
     openRiskAlert,
     markAlertRead,
   };
 }