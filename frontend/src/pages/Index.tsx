import { useState } from "react";
import { useChatState } from "@/hooks/useChatState";
import { AppSidebar } from "@/components/chat/AppSidebar";
import { ChatWindow } from "@/components/chat/ChatWindow";

const Index = () => {
  const {
    conversations,
    activeConversation,
    activeId,
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
      />
      <ChatWindow
        conversation={activeConversation}
        isTyping={isTyping}
        onSend={sendMessage}
        onToggleSidebar={() => setSidebarOpen(true)}
      />
    </div>
  );
};

export default Index;
