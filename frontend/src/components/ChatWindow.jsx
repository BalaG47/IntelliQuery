import { useState, useRef, useEffect } from "react";
import axios from "axios";
import ReactMarkdown from "react-markdown";
import { FiSend } from "react-icons/fi";

const ChatWindow = ({ onFirstMessage }) => {
  const [messages, setMessages] = useState([
    {
      sender: "ai",
      text: "Ask me anything about ocean data, and I'll visualize it!",
      timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const messagesEndRef = useRef(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  const sendMessage = async () => {
    if (!input.trim() || loading) return;

    if (messages.length === 1 && onFirstMessage) {
      onFirstMessage();
    }

    const userMessage = {
      sender: "user",
      text: input,
      timestamp: new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
    };

    setMessages((prev) => [...prev, userMessage]);
    const currentInput = input;
    setInput("");
    setLoading(true);

    try {
      const res = await axios.post("http://localhost:5000/get-question", {
        prompt: currentInput,
      });

      const aiMessage = {
        sender: "ai",
        text: res.data.response || "No response received.",
        data: res.data.data || [],
        timestamp: new Date().toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        }),
      };

      setMessages((prev) => [...prev, aiMessage]);
    } catch (error) {
      console.error("Error fetching response:", error);

      setMessages((prev) => [
        ...prev,
        {
          sender: "ai",
          text: "⚠️ Error fetching response from server.",
          data: [],
          timestamp: new Date().toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          }),
        },
      ]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex flex-col h-full bg-surface-0 text-primary rounded-[3px] border border-subtle shadow-none overflow-hidden">
      {/* Header — flat bar on surface-1 with hairline bottom border, no gradient */}
      <div className="p-3.5 border-b border-subtle bg-surface-1 flex items-center justify-between shrink-0">
        <h2 className="text-sm font-bold font-heading tracking-tight text-primary">
          IntelliQuery AI Chat
        </h2>
        {/* Square indicator in state-success + mono connected label */}
        <div className="flex items-center gap-1.5 font-mono text-xs text-secondary">
          <span className="w-2 h-2 rounded-[1px] bg-state-success shrink-0" />
          <span>Connected</span>
        </div>
      </div>

      {/* Messages list — scrolls inside its own container */}
      <div className="flex-1 p-4 overflow-y-auto space-y-3.5 min-h-0">
        {messages.map((msg, idx) => (
          <div
            key={idx}
            className={`flex flex-col ${msg.sender === "user" ? "items-end" : "items-start"}`}
          >
            {/* Sender and timestamp header in mono face, muted */}
            <div className="flex items-center gap-2 mb-1 px-1 font-mono text-[11px] text-muted">
              <span className="font-semibold uppercase tracking-wider text-secondary">
                {msg.sender === "ai" ? "IntelliQuery" : "You"}
              </span>
              <span>·</span>
              <span>{msg.timestamp}</span>
            </div>

            {/* Message panel — 2-4px radius, flat border */}
            <div
              className={`rounded-[3px] p-3.5 border max-w-[85%] text-xs leading-relaxed break-words ${
                msg.sender === "user"
                  ? "bg-accent-surface border-accent/40 text-primary self-end"
                  : "bg-surface-0 border-subtle text-primary self-start shadow-none"
              }`}
            >
              {msg.sender === "ai" ? (
                <div className="space-y-2">
                  <ReactMarkdown
                    components={{
                      h1: ({ node, ...props }) => (
                        <h1 className="text-sm font-bold font-heading text-primary my-2" {...props} />
                      ),
                      h2: ({ node, ...props }) => (
                        <h2 className="text-xs font-bold font-heading text-primary my-1.5" {...props} />
                      ),
                      h3: ({ node, ...props }) => (
                        <h3 className="text-xs font-semibold font-heading text-primary my-1" {...props} />
                      ),
                      p: ({ node, ...props }) => (
                        <p className="text-xs text-primary leading-relaxed my-1" {...props} />
                      ),
                      ul: ({ node, ...props }) => (
                        <ul className="list-disc list-inside text-xs text-secondary my-1 space-y-0.5" {...props} />
                      ),
                      ol: ({ node, ...props }) => (
                        <ol className="list-decimal list-inside text-xs text-secondary my-1 space-y-0.5" {...props} />
                      ),
                      li: ({ node, ...props }) => (
                        <li className="text-xs text-secondary" {...props} />
                      ),
                      code: ({ node, inline, className, children, ...props }) =>
                        inline ? (
                          <code
                            className="px-1 py-0.5 rounded-[2px] bg-surface-1 border border-subtle font-mono text-[11px] text-accent"
                            {...props}
                          >
                            {children}
                          </code>
                        ) : (
                          <pre className="p-2 rounded-[2px] bg-surface-1 border border-subtle font-mono text-[11px] text-primary overflow-x-auto my-2">
                            <code {...props}>{children}</code>
                          </pre>
                        ),
                      table: ({ node, ...props }) => (
                        <div className="overflow-x-auto my-2 border border-subtle rounded-[2px]">
                          <table className="w-full text-left text-xs border-collapse" {...props} />
                        </div>
                      ),
                      thead: ({ node, ...props }) => (
                        <thead className="bg-surface-1 border-b border-subtle text-secondary font-mono text-[10px] uppercase" {...props} />
                      ),
                      th: ({ node, ...props }) => (
                        <th className="px-2 py-1 font-medium" {...props} />
                      ),
                      td: ({ node, ...props }) => (
                        <td className="px-2 py-1 border-t border-subtle text-primary font-mono text-[11px]" {...props} />
                      ),
                    }}
                  >
                    {msg.text}
                  </ReactMarkdown>
                </div>
              ) : (
                <p className="text-xs whitespace-pre-wrap">{msg.text}</p>
              )}

              {/* Profiles data table if attached */}
              {msg.sender === "ai" && Array.isArray(msg.data) && msg.data.length > 0 && (
                <div className="mt-3 rounded-[2px] border border-subtle bg-surface-1 p-2">
                  <p className="mb-2 text-[11px] font-mono font-medium text-accent uppercase tracking-wider">
                    Returned profiles ({msg.data.length})
                  </p>
                  <div className="max-h-60 overflow-auto rounded-[2px]">
                    <table className="w-full text-left text-xs">
                      <thead className="sticky top-0 bg-surface-2 text-secondary font-mono text-[10px] uppercase tracking-wider">
                        <tr>
                          <th className="px-2 py-1 font-medium">Profile</th>
                          <th className="px-2 py-1 font-medium">Cycle</th>
                          <th className="px-2 py-1 font-medium">Time</th>
                          <th className="px-2 py-1 font-medium">Latitude</th>
                          <th className="px-2 py-1 font-medium">Longitude</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-subtle text-primary font-mono text-[11px]">
                        {msg.data.map((profile, profileIndex) => (
                          <tr key={profile.profile_id ?? profileIndex} className="hover:bg-surface-2/60 transition-colors">
                            <td className="px-2 py-1 tabular-nums">{profile.profile_id ?? "—"}</td>
                            <td className="px-2 py-1 tabular-nums">{profile.cycle_number ?? "—"}</td>
                            <td className="px-2 py-1 whitespace-nowrap text-secondary">
                              {profile.profile_time ? String(profile.profile_time).slice(0, 19) : "—"}
                            </td>
                            <td className="px-2 py-1 tabular-nums text-secondary">{profile.latitude ?? "—"}</td>
                            <td className="px-2 py-1 tabular-nums text-secondary">{profile.longitude ?? "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          </div>
        ))}

        {/* Calm loading indicator (no bouncing dots) */}
        {loading && (
          <div className="flex items-center gap-2 p-2 font-mono text-xs text-muted">
            <span className="w-1.5 h-1.5 rounded-[1px] bg-accent animate-pulse" />
            <span>IntelliQuery is querying telemetry data...</span>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input bar — sharp rectangular input with 3:1 border, 1px accent focus ring, rectangular accent send button */}
      <div className="p-3 border-t border-subtle bg-surface-1 flex items-center gap-2 shrink-0">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about oceanographic data, floats, temperature..."
          className="flex-1 bg-surface-0 border border-strong text-primary placeholder:text-muted px-3 py-2 rounded-[2px] font-mono text-xs focus:outline-none focus:ring-1 focus:ring-accent focus:border-accent"
          onKeyDown={(e) => e.key === "Enter" && sendMessage()}
          disabled={loading}
        />
        <button
          type="button"
          onClick={sendMessage}
          disabled={loading || !input.trim()}
          className="bg-accent hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed text-accent-contrast px-3.5 py-2 rounded-[2px] font-mono text-xs font-medium transition-opacity flex items-center justify-center gap-1.5 cursor-pointer shrink-0"
          aria-label="Send message"
        >
          <FiSend className="text-xs" />
          <span className="hidden sm:inline uppercase tracking-wider text-[11px]">Send</span>
        </button>
      </div>
    </div>
  );
};

export default ChatWindow;
