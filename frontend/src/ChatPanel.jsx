import { useCallback, useEffect, useRef, useState } from "react";
import { getChat, syncChat, uploadFiles } from "./api.js";
import Quiz from "./Quiz.jsx";

const STATUS_LABEL = {
  empty: "No lectures yet",
  pending: "Indexing…",
  indexing: "Indexing…",
  ready: "Ready",
  failed: "Indexing failed",
};

const fmtSize = (b) => (b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.ceil(b / 1024)} KB`);

export default function ChatPanel({ chatId, onRenamed }) {
  const [files, setFiles] = useState([]);
  const [status, setStatus] = useState({ state: "empty", failed_docs: 0 });
  const [busy, setBusy] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState("");
  const lastName = useRef(null);

  const refresh = useCallback(async () => {
    try {
      const d = await getChat(chatId);
      setFiles(d.files);
      setStatus(d.status);
      if (lastName.current !== d.chat.name) {
        lastName.current = d.chat.name;
        onRenamed?.(d.chat);
      }
      if (d.status.state === "pending") syncChat(chatId).catch(() => {});
    } catch (e) {
      setError(e.message);
    }
  }, [chatId, onRenamed]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // While Bedrock is ingesting, poll every 4s.
  useEffect(() => {
    if (status.state !== "indexing" && status.state !== "pending") return;
    const id = setInterval(refresh, 4000);
    return () => clearInterval(id);
  }, [status.state, refresh]);

  async function onPick(e) {
    const picked = [...e.target.files];
    e.target.value = "";
    if (!picked.length) return;
    setBusy(true);
    setError("");
    try {
      const r = await uploadFiles(chatId, picked);
      setStatus(r.status);
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function retry() {
    setError("");
    try {
      setStatus(await syncChat(chatId));
    } catch (e) {
      setError(e.message);
    }
  }

  const ready = status.state === "ready";

  return (
    <div className="panel">
      {error && <div className="banner error">{error}</div>}

      {!playing && (
        <section className="card">
          <div className="card-head">
            <h2>Lectures</h2>
            <span className={`chip ${status.state}`}>{busy ? "Uploading…" : STATUS_LABEL[status.state]}</span>
          </div>

          <label className={`drop ${busy ? "disabled" : ""}`}>
            <input type="file" multiple accept=".pdf,.docx,.doc,.txt,.md,.html,.csv,.xlsx,.xls" onChange={onPick} disabled={busy} />
            <strong>{busy ? "Uploading to S3…" : "Click to upload lectures"}</strong>
            <span>PDF, Word, TXT, MD, HTML, CSV, Excel · up to 25 MB each · export slides (.pptx) to PDF first</span>
          </label>

          {files.length > 0 && (
            <ul className="files">
              {files.map((f) => (
                <li key={f.name}>
                  <span>📄 {f.name}</span>
                  <span className="muted">{fmtSize(f.size)}</span>
                </li>
              ))}
            </ul>
          )}

          {status.state === "failed" && (
            <p className="hint">
              Bedrock could not index everything{status.failed_docs ? ` (${status.failed_docs} file(s) failed)` : ""}.{" "}
              <button className="link" onClick={retry}>Retry indexing</button>
            </p>
          )}
          {status.state === "ready" && status.failed_docs > 0 && (
            <p className="hint">{status.failed_docs} file(s) could not be indexed (unsupported or empty).</p>
          )}
        </section>
      )}

      <Quiz chatId={chatId} ready={ready} onPlayingChange={setPlaying} />
    </div>
  );
}
