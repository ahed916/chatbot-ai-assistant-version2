import { useState } from "react";
import { Plus, Search, X, Trash2, LogOut } from "lucide-react";
import { Conversation } from "@/types/chat";
import { ChatItem } from "./ChatItem";

interface AppSidebarProps {
  conversations: Conversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onDelete?: (id: string) => void;
  onLogout?: () => void;
  isOpen: boolean;
  onClose: () => void;
}

export function AppSidebar({
  conversations,
  activeId,
  onSelect,
  onNewChat,
  onDelete,
  onLogout,
  isOpen,
  onClose,
}: AppSidebarProps) {
  const [search, setSearch] = useState("");

  const filtered = conversations.filter(
    (c) =>
      c.title.toLowerCase().includes(search.toLowerCase()) ||
      c.messages.some((m) =>
        m.content.toLowerCase().includes(search.toLowerCase())
      )
  );

  return (
    <>
      {isOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-40 lg:hidden"
          onClick={onClose}
        />
      )}

      <aside
        className={`fixed lg:static inset-y-0 left-0 z-50 w-72 bg-sidebar-bg flex flex-col transition-transform duration-300 ${
          isOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0"
        }`}
      >
        {/* Header */}
        <div className="p-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-primary flex items-center justify-center">
              <span className="text-primary-foreground font-bold text-sm">R</span>
            </div>
            <span className="text-sidebar-fg font-semibold text-lg">RedMind</span>
          </div>
          <button
            onClick={onClose}
            className="lg:hidden text-sidebar-muted hover:text-sidebar-fg transition-colors"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* New Chat Button */}
        <div className="px-3 mb-2">
          <button
            onClick={onNewChat}
            disabled={
              conversations.find((c) => c.id === activeId)?.messages.length === 0 &&
              activeId !== null
            }
            className="w-full flex items-center gap-2 px-3 py-2.5 rounded-lg border border-sidebar-hover text-sidebar-fg text-sm font-medium hover:bg-sidebar-hover transition-colors"
          >
            <Plus className="h-4 w-4" />
            New Chat
          </button>
        </div>

        {/* Search */}
        <div className="px-3 mb-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-sidebar-muted" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search chats..."
              className="w-full bg-sidebar-hover text-sidebar-fg placeholder:text-sidebar-muted text-sm rounded-lg pl-8 pr-3 py-2 outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
        </div>

        {/* Chat List */}
        <div className="flex-1 overflow-y-auto sidebar-scroll px-2 py-1 space-y-0.5">
          {filtered.length === 0 ? (
            <p className="text-center text-sidebar-muted text-xs mt-8">
              No conversations found
            </p>
          ) : (
            filtered.map((conv) => (
              <div key={conv.id} className="group relative">
                <ChatItem
                  conversation={conv}
                  isActive={conv.id === activeId}
                  onClick={() => onSelect(conv.id)}
                />
                {onDelete && (
                  <button
                    onClick={(e) => { e.stopPropagation(); onDelete(conv.id); }}
                    className="absolute right-2 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 text-sidebar-muted hover:text-red-400 transition-all"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            ))
          )}
        </div>

        {/* Footer */}
        <div className="p-3 border-t border-sidebar-hover">
          {onLogout && (
            <button
              onClick={onLogout}
              className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sidebar-muted hover:text-sidebar-fg hover:bg-sidebar-hover transition-colors text-sm mb-2"
            >
              <LogOut className="h-4 w-4" />
              Sign out
            </button>
          )}
          <p className="text-[10px] text-sidebar-muted text-center">
            RedMind × Redmine AI Assistant
          </p>
        </div>
      </aside>
    </>
  );
}