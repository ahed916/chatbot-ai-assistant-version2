import { useState, useRef, useEffect } from "react";
import { SendHorizontal } from "lucide-react";

interface ChatInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
}

export function ChatInput({ onSend, disabled }: ChatInputProps) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSend = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height =
        Math.min(textareaRef.current.scrollHeight, 150) + "px";
    }
  }, [value]);

  return (
    <div className="border-t bg-background px-4 py-3">
      <div className="max-w-3xl mx-auto">
        <div className="flex items-end gap-2 bg-card border rounded-2xl px-4 py-2 shadow-sm focus-within:ring-2 focus-within:ring-primary/30 transition-shadow">
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask RedMind anything about your Redmine projects..."
            rows={1}
            disabled={disabled}
            className="flex-1 resize-none bg-transparent text-sm text-foreground placeholder:text-muted-foreground outline-none py-1.5 max-h-[150px]"
          />
          <button
            onClick={handleSend}
            disabled={!value.trim() || disabled}
            className="p-2 rounded-xl bg-primary text-primary-foreground disabled:opacity-30 hover:opacity-90 transition-opacity shrink-0"
          >
            <SendHorizontal className="h-4 w-4" />
          </button>
        </div>
        <p className="text-[10px] text-muted-foreground text-center mt-2">
          RedMind can make mistakes. Verify important actions in Redmine.
        </p>
      </div>
    </div>
  );
}
