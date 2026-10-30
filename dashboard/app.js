// The dashboard UI: React 18 + htm, no build step. Reads the JSON API served by agentic_harness/dashboard.py.
const { useState, useEffect, useRef } = React;
const html = htm.bind(React.createElement);
const POLL_MS = 2000;

// ---------- data ----------

// ms = null fetches once (a finished attempt doesn't change)
function usePoll(url, ms = POLL_MS) {
  const [state, setState] = useState({ data: null, error: null, url: null });
  useEffect(() => {
    if (!url) return;
    let alive = true;
    let timer;
    const tick = async () => {
      if (!document.hidden) {
        try {
          const res = await fetch(url, { cache: "no-store" });
          const body = await res.json();
          if (alive) setState(res.ok ? { data: body, error: null, url } : { data: null, error: body.error || res.statusText, url });
        } catch (e) {
          if (alive) setState((s) => ({ ...s, error: "offline" }));
        }
      }
      if (alive && ms) timer = setTimeout(tick, ms);
    };
    tick();
    return () => { alive = false; clearTimeout(timer); };
  }, [url, ms]);
  // never show the previous URL's data while the new one loads
  return state.url === url ? state : { data: null, error: null };
}

function useHash() {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const onChange = () => setHash(location.hash);
    addEventListener("hashchange", onChange);
    return () => removeEventListener("hashchange", onChange);
  }, []);
  const [, , runId, , attemptId] = hash.split("/"); // #/runs/<run>/attempts/<attempt>
  return { runId: runId && decodeURIComponent(runId), attemptId: attemptId && Number(attemptId) };
}

// ---------- formatting ----------

const pct = (x) => (x == null ? "–" : `${Number((x * 100).toFixed(1))}%`);
const cost = (x) => (!x ? "$0" : x < 0.01 ? `$${x.toFixed(4)}` : `$${x.toFixed(2)}`);

function dur(s) {
  if (s == null) return "–";
  if (s < 60) return `${s.toFixed(1)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
}

function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const bytes = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);

function shellJoin(cmd) {
  if (!Array.isArray(cmd)) return String(cmd);
  return cmd.map((a) => (/^[\w@%+=:,./-]+$/.test(a) ? a : `'${a.replace(/'/g, `'\\''`)}'`)).join(" ");
}

// the list is narrow: "anthropic/claude-haiku-5-5" -> "claude-haiku-5-5" (the full name is in the run header)
const shortModel = (m) => (m || "").split("/").pop();

const scoreText = (v) => (v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));

// ---------- outcomes ----------

// pass/fail split graded trials; a partial score counts as fail here (the score column shows the number)
function trialKind(t) {
  if (t.state === "running") return "running";
  if (t.state !== "done") return "pending";
  if (t.outcome === "graded") return t.score_float >= 1 ? "pass" : "fail";
  return t.outcome;
}

const LABELS = {
  pass: "pass", fail: "fail", grader_error: "grader error", infra_error: "infra error",
  unsupported: "unsupported", running: "running", pending: "pending", graded: "graded",
};

const Pill = ({ kind, children }) => html`
  <span class=${`pill k-${kind}`}><span class="dot"></span>${children || LABELS[kind] || kind}</span>`;

function ProgressBar({ run }) {
  const segments = [
    ["pass", run.passed], ["fail", run.graded - run.passed], ["grader_error", run.grader_error],
    ["infra_error", run.infra_error], ["unsupported", run.unsupported], ["running", run.running],
  ];
  const title = segments.filter(([, n]) => n).map(([k, n]) => `${n} ${LABELS[k]}`).join(" · ") || "nothing done yet";
  return html`
    <div class="bar" title=${title} role="img" aria-label=${title}>
      ${segments.map(([k, n]) => n > 0 && html`<span key=${k} class=${`seg-${k}`} style=${{ width: `${(100 * n) / run.total}%` }}></span>`)}
    </div>`;
}

function progressText(run) {
  if (run.status === "failed") return "setup failed";
  if (run.status === "pending") return run.setup_claimed ? "setting up" : "queued";
  return `${run.done}/${run.total}`;
}

const isActive = (run) => run.status === "pending" || run.status === "running";

// ---------- top bar ----------

function TopBar({ workers, offline }) {
  const n = workers ? workers.length : 0;
  return html`
    <header class="topbar">
      <a class="brand" href="#/">harness</a>
      <div class="workers" title=${workers ? workers.map((w) => w.id).join("\n") : ""}>
        ${offline
          ? html`<span class="dot off"></span>server offline`
          : html`<span class=${`dot ${n ? "live" : ""}`}></span>${n ? `${n} worker${n > 1 ? "s" : ""}` : "no workers"}`}
      </div>
    </header>`;
}

// ---------- runs list ----------

function RunsList({ runs, selected }) {
  return html`
    <nav class="runs" aria-label="Runs">
      ${runs.map((r) => html`
        <a key=${r.id} class="run-row" href=${`#/runs/${encodeURIComponent(r.id)}`} aria-current=${r.id === selected ? "true" : "false"}>
          <div class="run-line">
            <span class="run-title" title=${r.model}>${r.benchmark} · ${r.agent} <span class="run-model">${shortModel(r.model)}</span></span>
            <span class="run-acc">${pct(r.accuracy)}</span>
          </div>
          <${ProgressBar} run=${r} />
          <div class="run-meta">
            <span>${progressText(r)} · ${cost(r.cost_usd)}</span>
            <span>${ago(r.created_at)}</span>
          </div>
        </a>`)}
    </nav>`;
}

// ---------- run detail ----------

function Stat({ value, label }) {
  return html`<div><div class="stat-value">${value}</div><div class="stat-label">${label}</div></div>`;
}

function RunDetail({ runId, attemptId, noWorkers }) {
  const { data: run, error } = usePoll(`/api/runs/${encodeURIComponent(runId)}`);
  if (error && !run) return html`<p class="subtle">${error}</p>`;
  if (!run) return null;
  const wall = (run.finished_at || Date.now() / 1000) - run.created_at;
  const excluded = ["grader_error", "infra_error", "unsupported"].filter((k) => run[k] > 0);
  const open = (t) => t.attempts.length && (location.hash = `#/runs/${encodeURIComponent(run.id)}/attempts/${t.attempts[t.attempts.length - 1].id}`);
  return html`
    <section>
      <div class="head">
        <h1>${run.benchmark} · ${run.agent}</h1>
        <span class="subtle">${run.model}</span>
        ${run.status !== "done" && html`<${Pill} kind=${run.status === "failed" ? "fail" : "running"}>${progressText(run)}<//>`}
        <span class="spacer"></span>
        <span class="run-id mono">${run.id}</span>
      </div>
      <div class="stats">
        <${Stat} value=${pct(run.accuracy)} label="accuracy" />
        <${Stat} value=${`${run.graded}`} label=${`graded of ${run.total}`} />
        <${Stat} value=${cost(run.cost_usd)} label="cost" />
        <${Stat} value=${dur(wall)} label="wall time" />
      </div>
      ${excluded.length > 0 && html`
        <div class="excluded">
          <span class="subtle">Excluded</span>
          ${excluded.map((k) => html`<${Pill} key=${k} kind=${k}>${run[k]} ${LABELS[k]}<//>`)}
        </div>`}
      ${isActive(run) && noWorkers && html`
        <div class="notice">No worker running. Start one: <code>harness worker</code></div>`}
      <table>
        <thead>
          <tr><th>Task</th><th>Outcome</th><th>Score</th><th class="num">Attempts</th><th class="num">Cost</th><th class="num">Time</th></tr>
        </thead>
        <tbody>
          ${run.trials.map((t) => {
            const clickable = t.attempts.length > 0;
            const selected = t.attempts.some((a) => a.id === attemptId);
            return html`
              <tr key=${t.id} class=${clickable ? "clickable" : ""} aria-selected=${selected ? "true" : "false"}
                  tabindex=${clickable ? 0 : -1} onClick=${() => open(t)}
                  onKeyDown=${(e) => e.key === "Enter" && open(t)}>
                <td class="mono">${t.task_id}</td>
                <td><${Pill} kind=${trialKind(t)} /></td>
                <td class="mono">${scoreText(t.score_value)}</td>
                <td class="num">${t.attempt_count || ""}</td>
                <td class="num">${t.state === "done" || t.cost_usd ? cost(t.cost_usd) : ""}</td>
                <td class="num">${t.duration_s ? dur(t.duration_s) : ""}</td>
              </tr>`;
          })}
        </tbody>
      </table>
    </section>`;
}

// ---------- attempt drawer ----------

function Drawer({ runId, attemptId }) {
  const ref = useRef(null);
  const close = () => (location.hash = `#/runs/${encodeURIComponent(runId)}`);
  const [done, setDone] = useState(false);
  const { data: a, error } = usePoll(`/api/attempts/${attemptId}`, done ? null : POLL_MS);
  useEffect(() => setDone(false), [attemptId]);
  useEffect(() => { if (a && a.outcome) setDone(true); }, [a]);
  useEffect(() => {
    ref.current && ref.current.focus();
    const onKey = (e) => e.key === "Escape" && close();
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [attemptId]);
  const go = (id) => (location.hash = `#/runs/${encodeURIComponent(runId)}/attempts/${id}`);
  return html`
    <div class="backdrop" onClick=${close}></div>
    <aside class="drawer" ref=${ref} tabindex="-1" aria-label="Attempt">
      <div class="drawer-head">
        <h2 class="mono">${a ? a.task_id : ""}</h2>
        ${a && a.attempts.length > 1 && html`
          <div class="switcher" role="group" aria-label="Attempts">
            ${a.attempts.map((s) => html`
              <button key=${s.id} class="btn" aria-pressed=${s.id === a.id ? "true" : "false"} onClick=${() => go(s.id)}
                      title=${s.outcome || "running"}>
                <span class=${`pill k-${s.outcome || "running"}`} style=${{ padding: 0, background: "none" }}><span class="dot"></span></span>
                ${s.n}
              </button>`)}
          </div>`}
        <span class="spacer"></span>
        <button class="btn" onClick=${close} aria-label="Close">Close</button>
      </div>
      ${error && !a && html`<p class="subtle">${error}</p>`}
      ${a && html`<${Attempt} a=${a} />`}
    </aside>`;
}

function Attempt({ a }) {
  const graded = a.outcome === "graded";
  const kind = !a.outcome ? "running" : graded ? (a.outcome_file && a.outcome_file.score_float >= 1 ? "pass" : "fail") : a.outcome;
  const explanation = a.score && a.score.explanation;
  // transcript.json is written when the attempt ends; until then the trace's message events are the transcript
  const transcript = a.transcript ||
    (a.events || []).filter((e) => e.kind === "message_sent" || e.kind === "message_received").map((e) => e.payload);
  return html`
    <div class="facts">
      <${Pill} kind=${kind} />
      ${a.score && html`<span>score <b class="mono">${scoreText(a.score.value)}</b></span>`}
      ${a.exit_status && html`<span>agent exit <b>${a.exit_status}</b></span>`}
      ${a.outcome && html`<span><b>${cost(a.cost_usd)}</b></span>`}
      ${a.duration_s != null && html`<span><b>${dur(a.duration_s)}</b></span>`}
      ${a.attempts.length > 1 && html`<span>attempt ${a.n} of ${a.attempts.length}</span>`}
    </div>
    ${a.error && html`<pre class="error-box mono">${a.error}</pre>`}
    ${explanation && html`
      <details class="grader">
        <summary>Grader output</summary>
        <pre class="out">${explanation}</pre>
      </details>`}
    <h3>Transcript</h3>
    ${transcript.length
      ? transcript.map((m, i) => html`<${MessageView} key=${i} m=${m} />`)
      : html`<p class="faint">No messages yet</p>`}
    <h3>Trace</h3>
    <${Trace} events=${a.events || []} />
    ${a.files.length > 0 && html`
      <h3>Files</h3>
      <div class="files">
        ${a.files.map((f) => html`
          <a key=${f.name} href=${`/api/attempts/${a.id}/files/${encodeURIComponent(f.name)}`} target="_blank" rel="noopener">
            ${f.name} <span class="faint">${bytes(f.bytes)}</span>
          </a>`)}
      </div>`}`;
}

function contentText(c) {
  if (c.text != null) return c.text;
  if (c.path != null) return `[file] ${c.path}`;
  return JSON.stringify(c.data !== undefined ? c.data : c, null, 2);
}

function MessageView({ m }) {
  const text = (m.content || []).map(contentText).join("\n\n");
  return html`
    <div class=${`msg msg-${m.role}`}>
      <div class="msg-role">${m.role}</div>
      <div class="msg-text">${text || html`<span class="faint">(empty)</span>`}</div>
    </div>`;
}

// ---------- trace timeline ----------

function Trace({ events }) {
  const shown = events.filter((e) => e.kind !== "message_sent" && e.kind !== "message_received" && e.kind !== "tool_result");
  if (!shown.length) return html`<p class="faint">No commands yet</p>`;
  const results = {};
  for (const e of events) if (e.kind === "tool_result") results[e.payload.id] = e.payload;
  const t0 = events[0].ts;
  const turns = new Set(shown.map((e) => e.turn));
  let lastTurn = null;
  return html`
    <div class="trace">
      ${shown.map((e) => {
        const divider = turns.size > 1 && e.turn !== lastTurn && html`<div class="turn" key=${`turn-${e.turn}`}>Turn ${e.turn}</div>`;
        lastTurn = e.turn;
        return [divider, html`<${Event} key=${e.seq} e=${e} result=${results[e.payload && e.payload.id]} t0=${t0} />`];
      })}
    </div>`;
}

const offset = (e, t0) => `+${dur(e.ts - t0)}`;

function Output({ label, text }) {
  if (!text) return null;
  return html`${label && html`<div class="out-label">${label}</div>`}<pre class="out">${text}</pre>`;
}

function Event({ e, result, t0 }) {
  const p = e.payload || {};
  if (e.kind === "command") {
    const bad = p.error || p.timed_out || (p.exit_code != null && p.exit_code !== 0);
    const code = p.error ? html`<span class="code bad">error</span>`
      : p.timed_out ? html`<span class="code warn">timeout</span>`
      : html`<span class=${`code ${p.exit_code ? "bad" : ""}`}>${p.exit_code}</span>`;
    return html`
      <details class=${`ev ${bad ? "bad" : ""}`}>
        <summary>${code}<span class="ev-cmd mono">${shellJoin(p.command)}</span>
          <span class="ev-time">${p.duration_s != null ? dur(p.duration_s) : ""}</span><span class="ev-time">${offset(e, t0)}</span></summary>
        ${Array.isArray(p.command) && html`<${Output} label="command" text=${shellJoin(p.command)} />`}
        <${Output} label="stdout" text=${p.stdout} />
        <${Output} label="stderr" text=${p.stderr} />
        <${Output} label="error" text=${p.error} />
        ${!p.stdout && !p.stderr && !p.error && html`<div class="out-label">no output</div>`}
        ${p.truncated && html`<div class="out-label">output truncated; the full log is in the agent's trajectory file</div>`}
      </details>`;
  }
  if (e.kind === "tool_call") {
    const input = p.input || {};
    const summary = input.command || input.file_path || input.path || input.url || JSON.stringify(input);
    const bad = result && result.is_error;
    return html`
      <details class=${`ev ${bad ? "bad" : ""}`}>
        <summary>
          ${!result ? html`<span class="code">…</span>` : bad ? html`<span class="code bad">error</span>` : html`<span class="code">ok</span>`}
          <span class="ev-tool">${p.name}</span><span class="ev-cmd mono">${String(summary)}</span>
          <span class="ev-time">${offset(e, t0)}</span>
        </summary>
        ${input.command
          ? html`<${Output} label=${input.description || "command"} text=${input.command} />`
          : html`<${Output} label="input" text=${JSON.stringify(input, null, 2)} />`}
        ${result && html`<${Output} label="result" text=${typeof result.content === "string" ? result.content : JSON.stringify(result.content, null, 2)} />`}
        ${result && result.truncated && html`<div class="out-label">result truncated</div>`}
      </details>`;
  }
  if (e.kind === "workspace_saved") {
    return html`<div class="ev"><div class="row"><span class="faint">workspace saved · ${bytes(p.bytes || 0)}${p.truncated ? " (truncated)" : ""}</span>
      <span class="spacer"></span><span class="ev-time">${offset(e, t0)}</span></div></div>`;
  }
  return html`
    <details class="ev">
      <summary><span class="code">${e.kind}</span><span class="ev-cmd mono faint">${JSON.stringify(p)}</span><span class="ev-time">${offset(e, t0)}</span></summary>
      <${Output} text=${JSON.stringify(p, null, 2)} />
    </details>`;
}

// ---------- app ----------

function Empty() {
  return html`
    <div class="empty">
      <div>
        <h2>No runs yet</h2>
        <code>harness run --agent mini-swe-agent --benchmark bash-operations --wait</code>
      </div>
    </div>`;
}

function App() {
  const { runId, attemptId } = useHash();
  const runs = usePoll("/api/runs");
  const workers = usePoll("/api/workers");
  const list = runs.data || [];
  const selected = runId || (list[0] && list[0].id);
  const offline = runs.error === "offline";
  return html`
    <div class="app">
      <${TopBar} workers=${workers.data} offline=${offline} />
      ${runs.data && !list.length
        ? html`<${Empty} />`
        : html`
          <div class="main">
            <${RunsList} runs=${list} selected=${selected} />
            <main class="detail">
              ${selected && html`<${RunDetail} key=${selected} runId=${selected} attemptId=${attemptId} noWorkers=${workers.data && !workers.data.length} />`}
            </main>
          </div>`}
      ${runId && attemptId && html`<${Drawer} runId=${runId} attemptId=${attemptId} />`}
    </div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
