import { useState, useEffect } from "react";
import { supabase } from "@/lib/supabase";

export interface ChatLog {
  id: string;
  userId: string;
  userEmail: string;
  message: string;
  response: string;
  timestamp: Date;
}

// Call this from useChatState after each successful bot reply
export async function saveChatLog(
  userId: string,
  userEmail: string,
  message: string,
  response: string
): Promise<void> {
  await supabase.from("chat_logs").insert({
    user_id: userId,
    user_email: userEmail,
    message,
    response,
  });
}

// Used by the Admin dashboard
export function useChatLogs() {
  const [logs, setLogs] = useState<ChatLog[]>([]);

  useEffect(() => {
    supabase
      .from("chat_logs")
      .select("*")
      .order("timestamp", { ascending: false })
      .limit(50)
      .then(({ data }) => {
        if (!data) return;
        setLogs(
          data.map((row) => ({
            id: row.id,
            userId: row.user_id,
            userEmail: row.user_email,
            message: row.message,
            response: row.response ?? "",
            timestamp: new Date(row.timestamp),
          }))
        );
      });
  }, []);

  return { logs };
}