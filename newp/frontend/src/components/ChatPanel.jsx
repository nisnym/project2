import { useState, useRef, useEffect } from "react";
import { sendChatMessage } from "../api";

const STARTER_PROMPTS = [
  "How much did I spend on dining?",
  "Am I over budget anywhere?",
  "What budget should I set for groceries?",
  "Any advice on my spending?",
];

const OPENING = {
  role: "assistant",
  answer:
    "Ask about your spending, a budget, or what to do next. Every answer here shows the " +
    "reasoning behind it and the transactions it came from.",
};

export default function ChatPanel({ customerId, expanded = false }) {
  const [messages, setMessages] = useState([OPENING]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const scrollRef = useRef(null);

  // A new customer is a new conversation — carrying one person's context into
  // another person's account would be a data leak, not a feature.
  useEffect(() => {
    setMessages([OPENING]);
  }, [customerId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, loading]);

  async function send(text) {
    const message = text ?? input;
    if (!message.trim() || loading) return;

    const history = messages
      .filter((m) => m.answer && m !== OPENING)
      .slice(-6)
      .map((m) => ({ role: m.role, content: m.answer }));

    setMessages((m) => [...m, { role: "user", answer: message }]);
    setInput("");
    setLoading(true);

    const response = await sendChatMessage(customerId, message, history);

    setMessages((m) => [
      ...m,
      {
        role: "assistant",
        answer: response.answer,
        reasoning: response.reasoning,
        confidence: response.confidence,
        dataPoints: response.data_points_used,
        source: response.source,
      },
    ]);
    setLoading(false);
  }

  return (
    <div className={`chat-panel box ${expanded ? "chat-panel--expanded" : ""}`}>
      <div className="chat-header eyebrow">advisor / chat</div>

      <div className="chat-scroll" ref={scrollRef}>
        {messages.map((m, i) => (
          <div key={i} className={`chat-msg chat-msg--${m.role}`}>
            <span className="chat-msg-role eyebrow">
              {m.role === "user" ? "you" : "advisor"}
            </span>
            <p className="chat-msg-text">{m.answer}</p>

            {m.reasoning && (
              <div className="chat-reason">
                <span className="eyebrow">why</span>
                <p className="chat-reason-text mono">{m.reasoning}</p>

                {m.dataPoints?.length > 0 && (
                  <div className="chat-datapoints">
                    {m.dataPoints.map((point, index) => (
                      <span className="chat-datapoint mono" key={index}>
                        {point.label}: {point.value}
                        {point.source ? ` · ${point.source}` : ""}
                      </span>
                    ))}
                  </div>
                )}

                <div className="chat-tags">
                  {m.confidence && (
                    <span className={`confidence-tag confidence-tag--${m.confidence}`}>
                      {m.confidence} confidence
                    </span>
                  )}
                  {m.source && <span className="source-tag mono">{m.source}</span>}
                </div>
              </div>
            )}
          </div>
        ))}

        {loading && (
          <div className="chat-msg chat-msg--assistant">
            <span className="chat-msg-role eyebrow">advisor</span>
            <p className="chat-msg-text chat-typing mono">reading your ledger…</p>
          </div>
        )}
      </div>

      <div className="chat-prompts">
        {STARTER_PROMPTS.map((p) => (
          <button key={p} className="prompt-chip" onClick={() => send(p)}>
            {p}
          </button>
        ))}
      </div>

      <form
        className="chat-input-row"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <input
          className="chat-input mono"
          placeholder="Ask about your money…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
        <button className="chat-send" type="submit" disabled={loading}>
          send
        </button>
      </form>
    </div>
  );
}
