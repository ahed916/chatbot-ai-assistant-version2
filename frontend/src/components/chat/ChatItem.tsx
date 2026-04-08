import { Conversation } from "@/types/chat";
import { MessageSquare } from "lucide-react";

interface ChatItemProps {
  conversation: Conversation;
  isActive: boolean;
  onClick: () => void;
}

export function ChatItem({ conversation, isActive, onClick }: ChatItemProps) {
  const lastMessage = conversation.messages[conversation.messages.length - 1];
  const preview = lastMessage?.content.slice(0, 50) || "No messages yet";

  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-3 py-2.5 rounded-lg transition-colors group ${
        isActive ? "bg-sidebar-active" : "hover:bg-sidebar-hover"
      }`}
    >
      <div className="flex items-start gap-2.5">
        <MessageSquare className="h-4 w-4 mt-0.5 shrink-0 text-sidebar-muted" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-sidebar-fg truncate">
            {conversation.title}
          </p>
          <p className="text-xs text-sidebar-muted truncate mt-0.5">
            {preview}
          </p>
        </div>
      </div>
    </button>
  );
}
