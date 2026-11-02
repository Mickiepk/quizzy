import { useCallback, useEffect, useState } from "react";
import { createChat, deleteChat, listChats } from "./api.js";
import ChatPanel from "./ChatPanel.jsx";

export default function App() {
  const [chats, setChats] = useState([]);
  const [maxChats, setMaxChats] = useState(3);
  const [activeId, setActiveId] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listChats()
      .then(({ chats, max_chats }) => {
        setChats(chats);
        setMaxChats(max_chats);
        setActiveId(chats[0]?.id ?? null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  async function newChat() {
    setError("");
    try {
      const chat = await createChat();
      setChats((c) => [...c, chat]);
      setActiveId(chat.id);
    } catch (e) {
      setError(e.message);
    }
  }

  async function removeChat(id) {
    if (!window.confirm("Delete this chat and its uploaded lectures?")) return;
    setError("");
    try {
      await deleteChat(id);
      const rest = chats.filter((c) => c.id !== id);
      setChats(rest);
      if (activeId === id) setActiveId(rest[0]?.id ?? null);
    } catch (e) {
      setError(e.message);
    }
  }

  const rename = useCallback(
    (chat) => setChats((cs) => cs.map((c) => (c.id === chat.id ? { ...c, name: chat.name } : c))),
    []
  );

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1 className="logo">Quizzy<span>!</span></h1>
        <button className="primary wide" onClick={newChat} disabled={chats.length >= maxChats}>
          + New chat ({chats.length}/{maxChats})
        </button>
        <ul className="chat-list">
          {chats.map((c) => (
            <li key={c.id} className={c.id === activeId ? "active" : ""}>
              <button className="chat-name" onClick={() => setActiveId(c.id)} title={c.name}>
                {c.name}
              </button>
              <button className="icon" onClick={() => removeChat(c.id)} title="Delete chat">
                ✕
              </button>
            </li>
          ))}
        </ul>
        {chats.length >= maxChats && <p className="hint">Limit reached. Delete a chat to start a new one.</p>}
      </aside>

      <main className="main">
        {error && <div className="banner error">{error}</div>}
        {loading ? (
          <p className="hint">Loading…</p>
        ) : activeId ? (
          <ChatPanel key={activeId} chatId={activeId} onRenamed={rename} />
        ) : (
          <div className="empty">
            <h2>Turn your lectures into a quiz game</h2>
            <p>Create a chat, upload your lecture files, and Quizzy keeps the questions coming.</p>
          </div>
        )}
      </main>
    </div>
  );
}
