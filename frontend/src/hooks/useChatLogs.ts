import { useState, useCallback } from "react";

export interface ChatLog {
  id: string;
  userId: string;
  userEmail: string;
  message: string;
  response: string;
  timestamp: Date;
}

// In-memory store (shared across components via module scope)
let logsStore: ChatLog[] = [];
let listeners: Array<() => void> = [];

function notify() {
  listeners.forEach((fn) => fn());
}

export function addChatLog(log: ChatLog) {
  logsStore = [log, ...logsStore];
  notify();
}

export function useChatLogs() {
  const [logs, setLogs] = useState<ChatLog[]>(logsStore);

  // Subscribe to changes
  useState(() => {
    const listener = () => setLogs([...logsStore]);
    listeners.push(listener);
    return () => {
      listeners = listeners.filter((l) => l !== listener);
    };
  });

  return { logs };
}
