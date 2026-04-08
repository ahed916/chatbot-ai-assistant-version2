import { Bot, LayoutDashboard, ListChecks, Users } from "lucide-react";

interface EmptyStateProps {
  onSuggestion: (text: string) => void;
}

const suggestions = [
  { icon: ListChecks, text: "Create a bug in project Web Platform" },
  { icon: Users, text: "Assign John to issue #1023" },
  { icon: LayoutDashboard, text: "Show project activity dashboard for Q1" },
  { icon: Bot, text: "List all open issues in Mobile App project" },
];

export function EmptyState({ onSuggestion }: EmptyStateProps) {
  return (
    <div className="flex-1 flex flex-col items-center justify-center px-4">
      <div className="w-16 h-16 rounded-2xl bg-primary/10 flex items-center justify-center mb-6">
        <Bot className="h-8 w-8 text-primary" />
      </div>
      <h2 className="text-2xl font-semibold text-foreground mb-2">How can I help?</h2>
      <p className="text-muted-foreground text-sm mb-8 text-center max-w-md">
        I'm RedMind, your AI assistant for managing Redmine projects. Ask me to create issues, update statuses, assign users, or generate dashboards.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 w-full max-w-lg">
        {suggestions.map((s) => (
          <button
            key={s.text}
            onClick={() => onSuggestion(s.text)}
            className="flex items-center gap-3 px-4 py-3 rounded-xl border bg-card hover:bg-accent transition-colors text-left text-sm text-foreground"
          >
            <s.icon className="h-4 w-4 text-primary shrink-0" />
            <span>{s.text}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
