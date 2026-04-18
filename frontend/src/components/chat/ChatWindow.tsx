import { Conversation, RiskAlert } from "@/types/chat";
import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { EmptyState } from "./EmptyState";
import { RiskAlertBell } from "./RiskAlertBell";
import { PanelLeft } from "lucide-react";

interface ChatWindowProps {
  conversation: Conversation | null;
  isTyping: boolean;
  onSend: (text: string) => void;
  onToggleSidebar: () => void;
  riskAlerts: RiskAlert[];
  onOpenAlert: (id: string) => void;
  onDismissAlert: (id: string) => void;
}

export function ChatWindow({
  conversation,
  isTyping,
  onSend,
  onToggleSidebar,
  riskAlerts,
  onOpenAlert,
  onDismissAlert,
}: ChatWindowProps) {
  return (
    <div className="flex-1 flex flex-col min-w-0 h-screen">
      {/* Header */}
      <header className="h-14 border-b bg-background flex items-center px-4 gap-3 shrink-0">
        <button
          onClick={onToggleSidebar}
          className="lg:hidden text-muted-foreground hover:text-foreground transition-colors"
        >
          <PanelLeft className="h-5 w-5" />
        </button>

        <div className="flex items-center gap-2 flex-1 min-w-0">
          <div className="w-6 h-6 rounded-md bg-primary flex items-center justify-center lg:hidden">
            <span className="text-primary-foreground font-bold text-xs">R</span>
          </div>
          <h1 className="text-sm font-medium text-foreground truncate">
            {conversation ? conversation.title : "RedMind"}
          </h1>
        </div>

        {/* Bell lives here — right side of header */}
        <RiskAlertBell
          alerts={riskAlerts}
          onOpen={onOpenAlert}
          onMarkRead={onDismissAlert}
        />
      </header>

      {conversation && conversation.messages.length === 0 && !isTyping ? (
        <EmptyState onSuggestion={onSend} />
      ) : conversation ? (
        <MessageList messages={conversation.messages} isTyping={isTyping} />
      ) : (
        <EmptyState onSuggestion={onSend} />
      )}

      <ChatInput onSend={onSend} disabled={isTyping} />
    </div>
  );
}
