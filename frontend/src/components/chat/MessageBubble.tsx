/**
 * MessageBubble.tsx
 *
 * Renders a single chat message — either a user bubble, assistant text,
 * or a DashboardCard when the assistant returned a visual payload.
 *
 * The isDashboard flag is set by useChatState.appendBotMessage() AFTER
 * the full response arrives. During streaming the message shows as plain
 * text; once complete it flips to a visual card.
 */
import ReactMarkdown from "react-markdown";
import { ChatMessage } from "@/types/chat";
import { DashboardCard } from "./DashboardCard";
import { Bot, User } from "lucide-react";

interface MessageBubbleProps {
  message: ChatMessage;
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === "user";

  return (
    <div className={`flex gap-3 ${isUser ? "justify-end" : "justify-start"}`}>
      {!isUser && (
        <div className="w-8 h-8 rounded-full bg-primary flex items-center justify-center shrink-0 mt-1">
          <Bot className="h-4 w-4 text-primary-foreground" />
        </div>
      )}

      <div
        className={`min-w-0 ${
          isUser ? "max-w-[75%] order-first" : "w-full max-w-[90%]"
        }`}
      >
        {!isUser && (
          <span className="text-xs font-medium text-muted-foreground mb-1 block">
            RedMind
          </span>
        )}

        {isUser ? (
          // ── User bubble ──────────────────────────────────────────────────
          <div className="rounded-2xl px-4 py-2.5 text-sm leading-relaxed bg-chat-user text-chat-user-fg rounded-br-md">
            <p>{message.content}</p>
          </div>
        ) : message.isDashboard && message.dashboard ? (
          // ── Dashboard visual card ────────────────────────────────────────
          // message.dashboard is the parsed DashboardPayload object
          <DashboardCard dashboard={message.dashboard} />
        ) : (
          // ── Plain assistant text (markdown) ──────────────────────────────
          <div className="rounded-2xl px-4 py-2.5 text-sm leading-relaxed bg-chat-assistant text-chat-assistant-fg border rounded-bl-md shadow-sm">
            <div className="prose-chat">
              <ReactMarkdown>{message.content}</ReactMarkdown>
            </div>
          </div>
        )}

        <p
          className={`text-[10px] text-muted-foreground mt-1 ${
            isUser ? "text-right" : "text-left"
          }`}
        >
          {message.timestamp.toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          })}
          {message.latencyMs && !isUser && (
            <span className="ml-2 opacity-50">
              {(message.latencyMs / 1000).toFixed(1)}s
            </span>
          )}
        </p>
      </div>

      {isUser && (
        <div className="w-8 h-8 rounded-full bg-foreground/10 flex items-center justify-center shrink-0 mt-1">
          <User className="h-4 w-4 text-foreground" />
        </div>
      )}
    </div>
  );
}
