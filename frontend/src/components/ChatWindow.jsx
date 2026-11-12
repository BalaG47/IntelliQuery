import { useState, useRef, useEffect } from "react";
import axios from "axios";
import ReactMarkdown from "react-markdown";

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
    if (!input.trim()) return;

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
    setInput("");
    setLoading(true);

    try {
      const res = await axios.post(
        "http://localhost:5000/get-question",
        {
          prompt: input,
        }
      );

      const aiMessage = {
        sender: "ai",
        text: res.data.response,
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
    <div className="flex flex-col h-full bg-[#1E2128B3] text-white rounded-xl border border-[#FF1F4B33] shadow-lg overflow-hidden">

      {/* Header */}
      <div className="p-4 border-b border-gray-700 flex items-center justify-between bg-gradient-to-r from-blue-600 to-cyan-500">
        <h2 className="text-xl font-bold drop-shadow-md">🌊 IntelliQuery AI Chat</h2>
        <span className="text-gray-200 text-sm">Connected</span>
      </div>

      {/* Messages */}
      <div className="flex-1 p-4 overflow-y-auto space-y-4 scrollbar-thin scrollbar-thumb-blue-500 scrollbar-track-gray-800">
        {messages.map((msg, idx) => (
          <div
            key={idx}
            className={`flex items-end ${msg.sender === "user" ? "justify-end" : "justify-start"}`}
          >
            
            {msg.sender === "ai" && (
              <div className="flex flex-col items-center mr-2">
                <div className="w-10 h-10 rounded-full bg-cyan-500 flex items-center justify-center text-white font-bold">
                  AI
                </div>
                <span className="text-xs text-gray-400 mt-1" style={{ marginTop: "6px" }}>IntelliQuery</span>
              </div>
            )}
            {msg.sender === "user" && (
              <div className="flex flex-col items-center  ml-2">
                <div className="w-10 h-10 rounded-full bg-blue-600 flex items-center justify-center text-white font-bold">
                  U
                </div>
                <span className="text-xs text-gray-400 mt-1" style={{ marginTop: "6px" }}>You</span>
              </div>
            )}

            {/* Message Bubble */}
            <div
              className={`ml-2 bg-gradient-to-br p-4 rounded-2xl shadow-md break-words transition-all duration-200 ${
                msg.sender === "user"
                  ? "from-blue-500 to-blue-600 text-white rounded-br-none"
                  : "from-gray-700 to-gray-800 text-gray-100 rounded-bl-none"
              }`}
              style={{ maxWidth: "70%" }}
            >
              {msg.sender === "ai" ? (
                <div className="prose prose-invert text-sm">
                  <ReactMarkdown>{msg.text}</ReactMarkdown>
                </div>
              ) : (
                <p className="text-sm">{msg.text}</p>
              )}
              {msg.sender === "ai" && Array.isArray(msg.data) && msg.data.length > 0 && (
                <div className="mt-3 rounded-lg border border-gray-600 bg-gray-900/60 p-2">
                  <p className="mb-2 text-xs font-semibold text-cyan-300">
                    Returned profiles ({msg.data.length})
                  </p>
                  <div className="max-h-64 overflow-auto rounded">
                    <table className="w-full text-left text-xs text-gray-200">
                      <thead className="sticky top-0 bg-gray-800 text-cyan-200">
                        <tr>
                          <th className="px-2 py-1">Profile</th>
                          <th className="px-2 py-1">Cycle</th>
                          <th className="px-2 py-1">Time</th>
                          <th className="px-2 py-1">Latitude</th>
                          <th className="px-2 py-1">Longitude</th>
                        </tr>
                      </thead>
                      <tbody>
                        {msg.data.map((profile, profileIndex) => (
                          <tr key={profile.profile_id ?? profileIndex} className="border-t border-gray-700">
                            <td className="px-2 py-1">{profile.profile_id ?? "—"}</td>
                            <td className="px-2 py-1">{profile.cycle_number ?? "—"}</td>
                            <td className="px-2 py-1 whitespace-nowrap">
                              {profile.profile_time ? String(profile.profile_time).slice(0, 19) : "—"}
                            </td>
                            <td className="px-2 py-1">{profile.latitude ?? "—"}</td>
                            <td className="px-2 py-1">{profile.longitude ?? "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
              <span className="text-xs text-gray-400 mt-1 block text-right">{msg.timestamp}</span>
            </div>
          </div>
        ))}
        {loading && (
          <div className="text-gray-400 text-sm italic animate-pulse">IntelliQuery is thinking...</div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="p-4 border-t border-gray-700 bg-gray-900 flex items-center gap-2 rounded-b-xl">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Type your message..."
          className="flex-1 bg-gray-800 text-white px-4 py-2 rounded-full outline-none focus:ring-2 focus:ring-cyan-400 placeholder-gray-400 transition"
          onKeyDown={(e) => e.key === "Enter" && sendMessage()}
        />
        <button
          onClick={sendMessage}
          className="bg-cyan-500 hover:bg-cyan-600 px-4 py-2 rounded-full transition-transform active:scale-95 text-white shadow-md"
        >
          ➤
        </button>
      </div>
    </div>
  );
};

export default ChatWindow;
