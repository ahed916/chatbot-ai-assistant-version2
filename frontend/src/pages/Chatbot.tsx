import { useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useChatState } from "@/hooks/useChatState";
import { AppSidebar } from "@/components/chat/AppSidebar";
import { ChatWindow } from "@/components/chat/ChatWindow";
import { Button } from "@/components/ui/button";
import { LogOut } from "lucide-react";

export default function Chatbot() {
  //const { user, logout } = useAuth();
  const {
    conversations,
    activeConversation,
    activeId,
    riskAlerts,
    openRiskAlert,
    markAlertRead,
    isTyping,
    createConversation,
    selectConversation,
    sendMessage,
  } = useChatState();
  const [sidebarOpen, setSidebarOpen] = useState(false);

  return (
    <div className="flex h-screen w-full overflow-hidden">
      <AppSidebar
        conversations={conversations}
        activeId={activeId}
        onSelect={(id) => {
          selectConversation(id);
          setSidebarOpen(false);
        }}
        onNewChat={() => {
          createConversation();
          setSidebarOpen(false);
        }}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        /*footerContent={
          <div className="border-t border-[hsl(var(--sidebar-border))] p-3 flex items-center justify-between">
            <span className="text-xs text-[hsl(var(--sidebar-muted))] truncate">
              {user?.email}
            </span>
            <Button
              variant="ghost"
              size="sm"
              onClick={logout}
              className="text-[hsl(var(--sidebar-muted))] hover:text-[hsl(var(--sidebar-fg))] h-7 px-2"
            >
              <LogOut className="h-3.5 w-3.5" />
            </Button>
          </div>
        }*/
      />
      <ChatWindow
        conversation={activeConversation}
        isTyping={isTyping}
        onSend={sendMessage}
        onToggleSidebar={() => setSidebarOpen(true)}
        riskAlerts={riskAlerts}
        onOpenAlert={openRiskAlert}
        onDismissAlert={markAlertRead}
      />
    </div>
  );
}
