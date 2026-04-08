import { Bot } from "lucide-react";

export function TypingIndicator() {
  return (
    <div className="flex gap-3 justify-start">
      <div className="w-8 h-8 rounded-full bg-primary flex items-center justify-center shrink-0">
        <Bot className="h-4 w-4 text-primary-foreground" />
      </div>
      <div>
        <span className="text-xs font-medium text-muted-foreground mb-1 block">
          RedMind
        </span>
        <div className="bg-chat-assistant border rounded-2xl rounded-bl-md px-4 py-3 shadow-sm flex gap-1.5 items-center">
          <span
            className="w-2 h-2 rounded-full bg-primary inline-block animate-bounce"
            style={{ animationDelay: "0ms" }}
          />
          <span
            className="w-2 h-2 rounded-full bg-primary inline-block animate-bounce"
            style={{ animationDelay: "150ms" }}
          />
          <span
            className="w-2 h-2 rounded-full bg-primary inline-block animate-bounce"
            style={{ animationDelay: "300ms" }}
          />
        </div>
      </div>
    </div>
  );
}
