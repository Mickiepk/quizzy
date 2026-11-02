import { useCallback, useEffect, useRef, useState } from "react";
import { explainAnswer, generateQuiz } from "./api.js";

const TIME_LIMIT = 20; // seconds per question
const BATCH = 5; // questions per request; we prefetch so the quiz never stops
const SHAPES = ["▲", "◆", "●", "■"];
const NO_EXPLAIN = { loading: false, text: "", sources: [] };
const FRESH_STATS = { score: 0, streak: 0, best: 0, answered: 0, correct: 0 };

export default function Quiz({ chatId, ready, onPlayingChange }) {
  const [phase, setPhase] = useState("idle"); // idle | loading | answering | revealed | summary
  const [queue, setQueue] = useState([]);
  const [current, setCurrent] = useState(null);
  const [selected, setSelected] = useState(null);
  const [timeLeft, setTimeLeft] = useState(TIME_LIMIT);
  const [stats, setStats] = useState(FRESH_STATS);
  const [result, setResult] = useState(null);
  const [explain, setExplain] = useState(NO_EXPLAIN);
  const [error, setError] = useState("");
  const [topic, setTopic] = useState("");
  const [difficulty, setDifficulty] = useState("medium");

  const seen = useRef([]);
  const fetching = useRef(false);
  const startedAt = useRef(0);
  const selRef = useRef(null);
  const reqId = useRef(0);
  const submitRef = useRef(null);

  useEffect(() => {
    onPlayingChange?.(phase !== "idle" && phase !== "summary");
  }, [phase, onPlayingChange]);

  // ---- keep a few questions ahead of the player
  const fetchMore = useCallback(async () => {
    if (fetching.current) return;
    fetching.current = true;
    try {
      const { questions } = await generateQuiz(chatId, {
        count: BATCH,
        topic,
        difficulty,
        seen: seen.current.slice(-40),
      });
      questions.forEach((q) => seen.current.push(q.question));
      setQueue((prev) => [...prev, ...questions]);
    } catch (e) {
      setError(e.message);
    } finally {
      fetching.current = false;
    }
  }, [chatId, topic, difficulty]);

  useEffect(() => {
    const active = phase === "loading" || phase === "answering" || phase === "revealed";
    if (active && queue.length <= 2 && !error) fetchMore();
  }, [phase, queue.length, error, fetchMore]);

  // ---- show the next question as soon as one is available
  useEffect(() => {
    if (phase !== "loading" || queue.length === 0) return;
    const [first, ...rest] = queue;
    setQueue(rest);
    setCurrent(first);
    setSelected(null);
    selRef.current = null;
    setResult(null);
    setExplain(NO_EXPLAIN);
    setTimeLeft(TIME_LIMIT);
    startedAt.current = Date.now();
    setPhase("answering");
  }, [phase, queue]);

  // ---- countdown (auto-submits whatever is selected when time is up)
  useEffect(() => {
    if (phase !== "answering") return;
    const id = setInterval(() => {
      const left = Math.max(0, TIME_LIMIT - (Date.now() - startedAt.current) / 1000);
      setTimeLeft(left);
      if (left <= 0) {
        clearInterval(id);
        submitRef.current?.();
      }
    }, 100);
    return () => clearInterval(id);
  }, [phase, current]);

  function submit() {
    if (phase !== "answering" || !current) return;
    const picked = selRef.current;
    const elapsed = Math.min(TIME_LIMIT, (Date.now() - startedAt.current) / 1000);
    const correct = picked === current.answer_index;
    const streak = correct ? stats.streak + 1 : 0;
    const base = correct ? Math.round(1000 * (1 - elapsed / TIME_LIMIT / 2)) : 0;
    const bonus = correct && streak >= 3 ? 100 : 0;

    setResult({ correct, picked, points: base + bonus, streak });
    setStats({
      score: stats.score + base + bonus,
      streak,
      best: Math.max(stats.best, streak),
      answered: stats.answered + 1,
      correct: stats.correct + (correct ? 1 : 0),
    });
    setPhase("revealed");

    // AI explanation, grounded in the lecture, written after every submit
    const my = ++reqId.current;
    setExplain({ loading: true, text: "", sources: [] });
    explainAnswer(chatId, {
      question: current.question,
      choices: current.choices,
      answer_index: current.answer_index,
      picked_index: picked,
    })
      .then((r) => my === reqId.current && setExplain({ loading: false, text: r.explanation, sources: r.sources }))
      .catch((e) => my === reqId.current && setExplain({ loading: false, text: `Couldn't load the explanation: ${e.message}`, sources: [] }));
  }
  submitRef.current = submit;

  function start() {
    seen.current = [];
    setQueue([]);
    setCurrent(null);
    setStats(FRESH_STATS);
    setError("");
    setPhase("loading");
  }

  const choose = (i) => {
    if (phase !== "answering") return;
    setSelected(i);
    selRef.current = i;
  };

  const accuracy = stats.answered ? Math.round((stats.correct / stats.answered) * 100) : 0;

  // ================= render =================
  if (phase === "idle" || phase === "summary") {
    return (
      <section className="card">
        {phase === "summary" ? (
          <>
            <h2>Round summary</h2>
            <div className="stats">
              <div><b>{stats.score}</b><span>points</span></div>
              <div><b>{stats.correct}/{stats.answered}</b><span>correct ({accuracy}%)</span></div>
              <div><b>{stats.best}</b><span>best streak</span></div>
            </div>
          </>
        ) : (
          <h2>Quiz</h2>
        )}

        <div className="row">
          <input type="text" value={topic} placeholder="Focus on a topic (optional)" onChange={(e) => setTopic(e.target.value)} />
          <select value={difficulty} onChange={(e) => setDifficulty(e.target.value)}>
            <option value="easy">Easy</option>
            <option value="medium">Medium</option>
            <option value="hard">Hard</option>
          </select>
        </div>

        <div className="row">
          {phase === "summary" && (
            <button className="primary" onClick={() => setPhase("loading")} disabled={!ready}>Keep going</button>
          )}
          <button className={phase === "summary" ? "secondary" : "primary"} onClick={start} disabled={!ready}>
            {phase === "summary" ? "New round" : "Start quiz"}
          </button>
        </div>
        {!ready && <p className="hint">Upload a lecture and wait for “Ready” to unlock the quiz.</p>}
        {error && <div className="banner error">{error}</div>}
      </section>
    );
  }

  if (phase === "loading" || !current) {
    return (
      <section className="card center">
        {error ? (
          <>
            <div className="banner error">{error}</div>
            <div className="row">
              <button className="primary" onClick={() => { setError(""); }}>Try again</button>
              <button className="secondary" onClick={() => setPhase("summary")}>End quiz</button>
            </div>
          </>
        ) : (
          <>
            <div className="spinner" />
            <p>Writing questions from your lectures…</p>
          </>
        )}
      </section>
    );
  }

  const revealed = phase === "revealed";
  return (
    <section className="game">
      <div className="game-top">
        <span className="pill">Q{stats.answered + (revealed ? 0 : 1)}</span>
        <span className="pill">🔥 {stats.streak}</span>
        <span className="pill score">{stats.score} pts</span>
      </div>

      <div className="timer"><div style={{ width: `${(timeLeft / TIME_LIMIT) * 100}%` }} /><span>{Math.ceil(timeLeft)}</span></div>

      <h2 className="question">{current.question}</h2>

      <div className="tiles">
        {current.choices.map((c, i) => {
          let state = "";
          if (revealed) state = i === current.answer_index ? "correct" : i === result.picked ? "wrong" : "dim";
          else if (selected === i) state = "selected";
          return (
            <button key={i} className={`tile t${i} ${state}`} disabled={phase !== "answering"} onClick={() => choose(i)}>
              <span className="shape">{SHAPES[i]}</span>
              <span className="label">{c}</span>
              {revealed && i === current.answer_index && <span className="mark">✓</span>}
              {revealed && i === result.picked && i !== current.answer_index && <span className="mark">✕</span>}
            </button>
          );
        })}
      </div>

      {!revealed && (
        <button className="primary big" onClick={submit} disabled={selected === null}>Submit</button>
      )}

      {revealed && (
        <>
          <div className={`result ${result.correct ? "good" : "bad"}`}>
            {result.picked === null ? "Time's up!" : result.correct ? `Correct! +${result.points}` : "Not quite"}
            {result.correct && result.streak >= 3 && <span> · {result.streak} in a row 🔥</span>}
          </div>

          <div className="card explain">
            <h3>AI explanation</h3>
            {explain.loading ? <div className="skeleton" /> : <p>{explain.text}</p>}
            {!explain.loading && explain.sources.length > 0 && <p className="muted">From: {explain.sources.join(", ")}</p>}
          </div>

          <div className="row">
            <button className="primary big" onClick={() => setPhase("loading")}>Continue →</button>
            <button className="secondary" onClick={() => setPhase("summary")}>End quiz</button>
          </div>
        </>
      )}
    </section>
  );
}
