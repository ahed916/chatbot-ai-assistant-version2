import { useEffect, useMemo, useState } from "react";
import { Search, MessageSquare, ChevronRight, Bot, User as UserIcon } from "lucide-react";
import { adminApi, PMUser, Conversation, ConversationMessage } from "@/lib/adminApi";
import { AdminLayout } from "@/components/admin/AdminLayout";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

export default function ConversationsPage() {
  const [users, setUsers] = useState<PMUser[]>([]);
  const [selectedUser, setSelectedUser] = useState<PMUser | null>(null);

  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedConv, setSelectedConv] = useState<Conversation | null>(null);

  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [loadingUsers, setLoadingUsers] = useState(true);
  const [loadingConvs, setLoadingConvs] = useState(false);
  const [loadingMsgs, setLoadingMsgs] = useState(false);
  const [search, setSearch] = useState("");

  // Load users on mount — api interceptor attaches the token automatically
  useEffect(() => {
    adminApi.listUsers()
      .then((u) => { setUsers(u); if (u.length) setSelectedUser(u[0]); })
      .finally(() => setLoadingUsers(false));
  }, []);

  // Load conversations on user change
  useEffect(() => {
    if (!selectedUser) return;
    setLoadingConvs(true);
    setSelectedConv(null);
    setMessages([]);
    adminApi.listConversations(selectedUser.id)
      .then((c) => { setConversations(c); if (c.length) setSelectedConv(c[0]); })
      .finally(() => setLoadingConvs(false));
  }, [selectedUser]);

  // Load messages on conversation change
  useEffect(() => {
    if (!selectedConv) return;
    setLoadingMsgs(true);
    adminApi.getMessages(selectedConv.id)
      .then(setMessages)
      .finally(() => setLoadingMsgs(false));
  }, [selectedConv]);

  const filteredUsers = useMemo(() => {
    const q = search.toLowerCase().trim();
    if (!q) return users;
    return users.filter(
      (u) => u.email.toLowerCase().includes(q) || u.full_name?.toLowerCase().includes(q),
    );
  }, [users, search]);

  return (
    <AdminLayout title="Conversations" description="Browse chat history for any project manager">
      <div className="grid grid-cols-12 gap-4 h-[calc(100vh-9.5rem)]">

        {/* Users column */}
        <div className="col-span-3 rounded-xl border bg-card flex flex-col overflow-hidden">
          <div className="p-3 border-b">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search users…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-8 h-9"
              />
            </div>
          </div>
          <div className="flex-1 overflow-y-auto chat-scroll">
            {loadingUsers ? (
              <div className="p-3 space-y-2">
                {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-12 w-full" />)}
              </div>
            ) : (
              <ul>
                {filteredUsers.map((u) => (
                  <li key={u.id}>
                    <button
                      onClick={() => setSelectedUser(u)}
                      className={cn(
                        "w-full text-left flex items-center gap-3 px-3 py-2.5 border-b transition-colors",
                        selectedUser?.id === u.id
                          ? "bg-primary/5 border-l-2 border-l-primary"
                          : "hover:bg-muted/40",
                      )}
                    >
                      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary text-xs font-semibold">
                        {u.full_name?.[0]?.toUpperCase() ?? u.email[0]?.toUpperCase()}
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-foreground truncate">
                          {u.full_name || u.email}
                        </p>
                        <p className="text-xs text-muted-foreground truncate">{u.email}</p>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* Conversations column */}
        <div className="col-span-3 rounded-xl border bg-card flex flex-col overflow-hidden">
          <div className="px-4 py-3 border-b">
            <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
              Conversations
            </p>
            <p className="text-sm font-semibold text-foreground truncate">
              {selectedUser?.full_name || selectedUser?.email || "—"}
            </p>
          </div>
          <div className="flex-1 overflow-y-auto chat-scroll">
            {loadingConvs ? (
              <div className="p-3 space-y-2">
                {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-14 w-full" />)}
              </div>
            ) : conversations.length === 0 ? (
              <div className="p-6 text-center">
                <MessageSquare className="h-8 w-8 text-muted-foreground/40 mx-auto mb-2" />
                <p className="text-xs text-muted-foreground">No conversations yet.</p>
              </div>
            ) : (
              <ul>
                {conversations.map((c) => (
                  <li key={c.id}>
                    <button
                      onClick={() => setSelectedConv(c)}
                      className={cn(
                        "w-full text-left px-3 py-3 border-b transition-colors flex items-center gap-2",
                        selectedConv?.id === c.id
                          ? "bg-primary/5 border-l-2 border-l-primary"
                          : "hover:bg-muted/40",
                      )}
                    >
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-foreground truncate">{c.title}</p>
                        <p className="text-xs text-muted-foreground">
                          {new Date(c.updated_at).toLocaleString()}
                        </p>
                      </div>
                      <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* Messages column */}
        <div className="col-span-6 rounded-xl border bg-card flex flex-col overflow-hidden">
          <div className="px-5 py-3 border-b">
            <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
              Messages
            </p>
            <p className="text-sm font-semibold text-foreground truncate">
              {selectedConv?.title || "Select a conversation"}
            </p>
          </div>
          <div className="flex-1 overflow-y-auto chat-scroll p-5 space-y-4 bg-background/40">
            {loadingMsgs ? (
              <div className="space-y-3">
                {Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-16 w-3/4" />
                ))}
              </div>
            ) : messages.length === 0 ? (
              <div className="h-full flex flex-col items-center justify-center text-center">
                <MessageSquare className="h-10 w-10 text-muted-foreground/40 mb-2" />
                <p className="text-sm text-muted-foreground">No messages in this conversation.</p>
              </div>
            ) : (
              messages.map((m, i) => (
                <div
                  key={i}
                  className={cn("flex gap-3", m.role === "user" ? "justify-end" : "justify-start")}
                >
                  {m.role === "assistant" && (
                    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary mt-0.5">
                      <Bot className="h-3.5 w-3.5" />
                    </div>
                  )}
                  <div
                    className={cn(
                      "max-w-[75%] rounded-2xl px-4 py-2.5 text-sm shadow-sm",
                      m.role === "user"
                        ? "bg-primary text-primary-foreground rounded-br-sm"
                        : "bg-card border rounded-bl-sm",
                    )}
                  >
                    <p className="whitespace-pre-wrap leading-relaxed">{m.content}</p>
                    <p
                      className={cn(
                        "text-[10px] mt-1.5",
                        m.role === "user" ? "text-primary-foreground/70" : "text-muted-foreground",
                      )}
                    >
                      {new Date(m.created_at).toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </p>
                  </div>
                  {m.role === "user" && (
                    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground mt-0.5">
                      <UserIcon className="h-3.5 w-3.5" />
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </AdminLayout>
  );
}