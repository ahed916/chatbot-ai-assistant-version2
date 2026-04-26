import { useState } from "react";
import { useChatState } from "@/hooks/useChatState";
import { AppSidebar } from "@/components/chat/AppSidebar";
import { ChatWindow } from "@/components/chat/ChatWindow";

export default function Chat() {
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
    deleteConversation,
    sendMessage,
    logout,
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
        onDelete={deleteConversation}
        onLogout={logout}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
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