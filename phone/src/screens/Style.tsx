import { useState } from "react";
import { ApiError, api, errorMessage, post, put } from "../api/client";
import type { QuizQuestion, StyleProfile } from "../api/types";
import { Empty, LoadError, Loading, Nav, Spinner, label, relTime } from "../components/ui";
import { toast, useLoad } from "../lib/hooks";

type Answers = Record<string, unknown>;

function Quiz({ initial, onDone, onCancel }: { initial: Answers; onDone: (p: StyleProfile) => void; onCancel: () => void }) {
  const qs = useLoad(() => api<{ questions: QuizQuestion[] }>("/api/style/quiz").then((r) => r.questions), []);
  const [i, setI] = useState(0);
  const [answers, setAnswers] = useState<Answers>(initial);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  if (qs.error) return <LoadError error={qs.error} retry={qs.reload} />;
  if (!qs.data) return <Loading />;
  const q = qs.data[i];
  const v = answers[q.id];
  const set = (x: unknown) => setAnswers((a) => ({ ...a, [q.id]: x }));
  const empty = v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0);
  const last = i === qs.data.length - 1;

  const submit = async () => {
    setBusy(true);
    setErrors([]);
    try {
      const clean: Answers = {};
      for (const [k, x] of Object.entries(answers)) if (!(x === "" || x == null || (Array.isArray(x) && !x.length))) clean[k] = x;
      onDone(await post<StyleProfile>("/api/style/quiz", { answers: clean }));
    } catch (e) {
      setErrors(e instanceof ApiError ? (Array.isArray(e.detail) ? (e.detail as string[]).map(String) : [e.message]) : [errorMessage(e)]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page has-bottom-bar no-tabs-pad">
      <div className="row-between">
        <span className="tiny">
          Question {i + 1} of {qs.data.length}
        </span>
        <button className="btn plain small" onClick={onCancel}>
          Close
        </button>
      </div>
      <div className="quiz-progress">
        <div style={{ width: `${((i + 1) / qs.data.length) * 100}%` }} />
      </div>
      <h2 className="quiz-q">{q.prompt}</h2>
      {q.kind === "single" &&
        q.options!.map((o) => (
          <button key={o.value} className={`option ${v === o.value ? "on" : ""}`} aria-pressed={v === o.value} onClick={() => set(o.value)}>
            {label(o.label)}
          </button>
        ))}
      {q.kind === "multi" && (
        <div className="toggle-grid">
          {q.options!.map((o) => {
            const arr = (v as string[] | undefined) ?? [];
            const on = arr.includes(o.value);
            return (
              <button
                key={o.value}
                className={`toggle ${on ? "on" : ""}`}
                aria-pressed={on}
                onClick={() => set(on ? arr.filter((x) => x !== o.value) : arr.length < (q.max_choices ?? 99) ? [...arr, o.value] : arr)}
              >
                {label(o.label)}
              </button>
            );
          })}
        </div>
      )}
      {q.kind === "scale" && (
        <>
          <div className="scale">
            {Array.from({ length: (q.max ?? 5) - (q.min ?? 1) + 1 }, (_, k) => (q.min ?? 1) + k).map((n) => (
              <button key={n} className={v === n ? "on" : ""} aria-pressed={v === n} onClick={() => set(n)}>
                {n}
              </button>
            ))}
          </div>
          <div className="row-between tiny mt">
            <span>Comfort</span>
            <span>Polish</span>
          </div>
        </>
      )}
      {q.kind === "text" && (
        <textarea className="input" maxLength={q.max_length} value={(v as string) ?? ""} onChange={(e) => set(e.target.value)} placeholder="Optional" />
      )}
      {!q.required && <p className="tiny mt">Optional: skip if nothing comes to mind.</p>}
      {errors.map((e) => (
        <div className="field-error" key={e}>
          {e}
        </div>
      ))}
      <div className="bottom-bar">
        <button className="btn big" disabled={i === 0} onClick={() => setI(i - 1)}>
          Back
        </button>
        {last ? (
          <button className="btn big primary" disabled={busy || (q.required && empty)} onClick={() => void submit()}>
            {busy ? <Spinner small /> : "Finish"}
          </button>
        ) : (
          <button className="btn big primary" disabled={q.required && empty} onClick={() => setI(i + 1)}>
            {empty && !q.required ? "Skip" : "Next"}
          </button>
        )}
      </div>
    </div>
  );
}

export function Style() {
  const profile = useLoad(() => api<StyleProfile>("/api/style"), [], ["style.updated"]);
  const history = useLoad(() => api<StyleProfile[]>("/api/style/history"), [], ["style.updated"]);
  const [quiz, setQuiz] = useState(false);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [fit, setFit] = useState("");
  const [busy, setBusy] = useState(false);

  if (quiz)
    return (
      <Quiz
        initial={profile.data?.quiz ?? {}}
        onCancel={() => setQuiz(false)}
        onDone={(p) => {
          profile.setData(() => p);
          void history.reload();
          setQuiz(false);
          toast({ text: "Style profile saved.", kind: "ok" });
        }}
      />
    );

  const p = profile.data;
  const save = async () => {
    setBusy(true);
    try {
      const r = await put<StyleProfile>("/api/style", { text, fit_notes: fit });
      profile.setData(() => r);
      void history.reload();
      setEditing(false);
      toast("Saved. Tomorrow's outfits will use it.");
    } catch (e) {
      toast({ text: errorMessage(e), kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <Nav
        title="Style"
        backTo="/more"
        right={
          p?.version && !editing ? (
            <button
              className="btn plain"
              onClick={() => {
                setText(p.text);
                setFit(p.fit_notes ?? "");
                setEditing(true);
              }}
            >
              Edit
            </button>
          ) : editing ? (
            <button className="btn plain" style={{ fontWeight: 600 }} disabled={busy} onClick={() => void save()}>
              {busy ? <Spinner small /> : "Save"}
            </button>
          ) : undefined
        }
      />
      <div className="page" style={{ paddingTop: 12 }}>
        {profile.error && !p ? (
          <LoadError error={profile.error} retry={profile.reload} />
        ) : !p ? (
          <Loading />
        ) : !p.version ? (
          <Empty icon="✨" title="Tell the mirror your style" action={<button className="btn primary big" onClick={() => setQuiz(true)}>Take the 2-minute quiz</button>}>
            A few questions about what you like to wear. The answers become a short profile you can edit any time.
          </Empty>
        ) : editing ? (
          <>
            <label className="field">
              <span>Your style, in plain words</span>
              <textarea className="input" style={{ minHeight: 180 }} maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} />
            </label>
            <label className="field">
              <span>Fit notes (used as written, never guessed from photos)</span>
              <textarea className="input" maxLength={300} value={fit} placeholder="e.g. Trousers are often too long on me" onChange={(e) => setFit(e.target.value)} />
            </label>
            <button className="btn block" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <div className="card pad">
              <div className="profile-text">{p.text}</div>
              <div className="tiny mt">
                Version {p.version} · {p.source === "quiz" ? "from the quiz" : p.source === "user" ? "edited by you" : "weekly update"} · {relTime(p.updated_at)}
              </div>
            </div>
            <div className="section-title">Fit notes</div>
            <div className="card pad small">{p.fit_notes || <span className="muted">None yet. Tap Edit to add how clothes usually fit you.</span>}</div>
            <button className="btn block mt2" onClick={() => setQuiz(true)}>
              Retake the quiz
            </button>
            {history.data && history.data.length > 1 && (
              <>
                <div className="section-title">History</div>
                <div className="list">
                  {history.data.map((h) => (
                    <details key={String(h.version)} className="list-row" style={{ display: "block" }}>
                      <summary style={{ cursor: "pointer", minHeight: 28 }}>
                        Version {h.version} · {label(h.source)} · {relTime(h.updated_at)}
                      </summary>
                      <div className="small muted profile-text" style={{ marginTop: 8 }}>
                        {h.text}
                      </div>
                    </details>
                  ))}
                </div>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
