import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const ACTIVE = new Set(["SUBMITTED", "SUBMITTING", "PENDING", "RUNNING", "COMPLETING", "CANCEL_REQUESTED"]);
const DASHBOARD_POLL_MS = 60_000;
const ACTIVE_JOB_POLL_MS = 20_000;

// 응답이 끝내 안 오면 사용자에게는 "눌러도 아무 일도 안 일어남"으로 보인다.
// 그건 가장 나쁜 실패 방식이라, 기다리다 포기하고 이유를 말해준다.
const API_TIMEOUT_MS = 45_000;

async function api(path, options = {}) {
  const { timeoutMs = API_TIMEOUT_MS, ...init } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(path, {
      ...init,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(init.headers || {}) },
    });
  } catch (cause) {
    const timedOut = cause?.name === "AbortError";
    const error = new Error(timedOut
      ? "서버가 제때 응답하지 않았습니다. SERAPH 연결이 끊겼을 수 있습니다 — 새로고침하거나 다시 연결해 주세요."
      : "서버에 연결하지 못했습니다.");
    error.code = timedOut ? "REQUEST_TIMEOUT" : "NETWORK_ERROR";
    error.retryable = true;
    throw error;
  } finally {
    clearTimeout(timer);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data?.error?.message || "요청을 처리하지 못했습니다.");
    error.code = data?.error?.code || "REQUEST_FAILED";
    error.retryable = Boolean(data?.error?.retryable);
    throw error;
  }
  return data;
}

function Icon({ name, size = 20 }) {
  const paths = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></>,
    plus: <><path d="M12 5v14M5 12h14"/></>,
    jobs: <><rect x="3" y="5" width="18" height="15" rx="3"/><path d="M8 5V3m8 2V3M3 10h18M8 15h3"/></>,
    gpu: <><rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 10h6v4H7zm10-1v6M6 3v3m4-3v3m4-3v3m4-3v3M6 18v3m4-3v3m4-3v3m4-3v3"/></>,
    refresh: <><path d="M20 11a8 8 0 1 0-2.34 5.66"/><path d="M20 4v7h-7"/></>,
    arrow: <><path d="m9 18 6-6-6-6"/></>,
    check: <><path d="m5 12 4 4L19 6"/></>,
    copy: <><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></>,
    folder: <><path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></>,
    terminal: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/></>,
    spark: <><path d="m12 3 1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6zM19 15l.7 2.3L22 18l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7z"/></>,
    close: <><path d="m6 6 12 12M18 6 6 18"/></>,
    server: <><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01M11 7h6M11 17h6"/></>,
    history: <><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l3.5 2"/></>,
    warn: <><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4m0 4h.01"/></>,
    book: <><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></>,
    bell: <><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></>,
    logout: <><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/></>,
    box: <><path d="M21 8v8a2 2 0 0 1-1 1.73l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 16V8a2 2 0 0 1 1-1.73l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8z"/><path d="m3.3 7 8.7 5 8.7-5M12 22V12"/></>,
    trash: <><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></>,
  };
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function StatusPill({ status }) {
  const key = (status || "UNKNOWN").toUpperCase();
  const labels = {
    STAGED: "준비 완료", SUBMITTING: "제출 중", SUBMITTED: "제출됨", PENDING: "대기 중",
    RUNNING: "실행 중", COMPLETING: "정리 중", COMPLETED: "완료", FAILED: "실패",
    CANCELLED: "취소됨", CANCEL_REQUESTED: "취소 요청", TIMEOUT: "시간 초과", OUT_OF_MEMORY: "메모리 부족",
  };
  return <span className={`status status-${key.toLowerCase()}`}><i />{labels[key] || key}</span>;
}

function Metric({ icon, label, value, detail, accent }) {
  return <article className="metric-card">
    <div className={`metric-icon ${accent || ""}`}><Icon name={icon} /></div>
    <div><p>{label}</p><strong>{value ?? "—"}</strong><span>{detail}</span></div>
  </article>;
}

// 내 할당량(QOS) — show-qos / sacctmgr 가 주는 값을 그대로 대시보드로 보여준다.
// 세라프에서 대기의 상당수는 GPU 부족이 아니라 이 한도 때문이라, 4종을 다 보여줘야 원인을 찾는다.
const QUOTA_ROWS = [
  { key: "gpu",  label: "GPU",          icon: "gpu",    used: (u) => u.gpus_in_use,      limit: (u) => u.gpus_limit },
  { key: "hp",   label: "고성능 GPU",    icon: "spark",  used: (u) => u.high_perf_in_use, limit: (u) => u.high_perf_limit },
  { key: "run",  label: "동시 실행 작업", icon: "jobs",   used: (u) => u.running_jobs,     limit: (u) => u.running_jobs_limit },
  { key: "sub",  label: "제출 작업",     icon: "server", used: (u) => u.submitted_jobs,   limit: (u) => u.submit_jobs_limit },
];

// limit===0 은 "권한 없음"(기본 QOS 의 high_perf=0), limit===null 은 한도 없음.
function quotaState(used, limit) {
  if (limit === 0) return "off";
  if (limit == null) return "free";
  if (used >= limit) return "full";
  return used / limit >= 0.8 ? "warn" : "ok";
}

function QuotaGauge({ label, icon, used, limit }) {
  const state = quotaState(used, limit);
  const pct = state === "off" || state === "free" ? 0 : Math.min(100, Math.round((used / limit) * 100));
  const note = state === "off" ? "사용 권한 없음"
    : state === "free" ? "한도 없음"
    : state === "full" ? "한도 도달 · 추가 제출은 대기"
    : `${limit - used}개 남음`;
  return <div className={`quota-cell ${state}`}>
    <div className="qc-head"><Icon name={icon} size={15}/><span>{label}</span></div>
    <div className="qc-value"><strong>{used ?? "—"}</strong><em>/ {limit == null ? "∞" : limit}</em></div>
    <div className="qc-bar"><i style={{ width: `${pct}%` }}/></div>
    <p className="qc-note">{note}</p>
  </div>;
}

function QuotaPanel({ usage, me }) {
  const hit = usage ? QUOTA_ROWS.filter((r) => quotaState(r.used(usage), r.limit(usage)) === "full") : [];
  const posLabel = me?.position === "undergrad" ? "학부" : me?.position === "grad" ? "대학원" : null;
  return <article className="panel quota-panel">
    <div className="panel-head">
      <div><p className="eyebrow">MY QUOTA</p><h2>내 할당량</h2></div>
      <div className="quota-meta">
        {usage?.qos && <span className="qos-badge">QOS {usage.qos}</span>}
        {(me?.account || posLabel) && <span className="qos-sub">{[me?.account, posLabel].filter(Boolean).join(" · ")}</span>}
      </div>
    </div>
    {!usage
      ? <div className="empty-mini"><Icon name="server" size={20}/><span>할당량을 불러오는 중입니다.</span></div>
      : <>
        <div className="quota-grid-4">
          {QUOTA_ROWS.map((r) => <QuotaGauge key={r.key} label={r.label} icon={r.icon} used={r.used(usage)} limit={r.limit(usage)}/>)}
        </div>
        <p className={`quota-note ${hit.length ? "hit" : ""}`}>
          <Icon name={hit.length ? "warn" : "check"} size={14}/>
          {hit.length
            ? `${hit.map((h) => h.label).join(" · ")} 한도에 도달했습니다 — 대기 중인 작업은 GPU 부족이 아니라 이 한도 때문일 수 있습니다.`
            : "여유가 있습니다. 세라프 대기의 상당수는 GPU 부족이 아니라 이 QOS 한도 때문입니다."}
        </p>
      </>}
  </article>;
}

// 접속 화면. 사용자에게 ariel/moana 중 무엇을 쓸지 묻지 않는다 — 학과 × 신분으로 이미
// 정해지는 값이라(서버 clusters.routing_table()), 학과·신분·교내외만 받고 호스트/포트는 자동으로 정한다.
// 규칙에서 벗어나는 계정을 위해 "직접 지정" 도 남겨둔다.
const FALLBACK_POSITIONS = [{ key: "undergrad", label: "학부생" }, { key: "grad", label: "대학원생" }];
const FALLBACK_PORTS = { on_campus: 22, off_campus: 30080 };

function ConnectCard({ mode, clusters, routing, health, loading, onConnect }) {
  const [username, setUsername] = useState(health?.ssh_username || "");
  const [major, setMajor] = useState("");
  const [position, setPosition] = useState("undergrad");
  const [offCampus, setOffCampus] = useState(true);
  const [manual, setManual] = useState(false);
  const [host, setHost] = useState(health?.ssh_host || "");
  const [port, setPort] = useState(health?.ssh_port ? String(health.ssh_port) : "");
  const [password, setPassword] = useState("");

  const ports = routing?.ssh_ports || FALLBACK_PORTS;
  const positions = routing?.positions?.length ? routing.positions : FALLBACK_POSITIONS;
  const autoCluster = routing && major ? routing.assign[`${major}:${position}`] : null;
  const autoHost = autoCluster ? clusters?.[autoCluster]?.host : null;
  const autoPort = offCampus ? ports.off_campus : ports.on_campus;
  const effHost = manual ? host : autoHost || "";
  const effPort = manual ? port : String(autoPort);
  const ready = mode !== "ssh" || Boolean(username && effHost && effPort);
  const submit = () => { if (ready && !loading) onConnect({ username, host: effHost, port: effPort, password }); };

  return <div className="connect-overlay"><div className="connect-card">
    <div className="connect-logo"><Icon name="server" size={30}/></div>
    <p className="eyebrow">SERAPH CONNECTION</p>
    <h2>서버 연결이 필요합니다</h2>
    <p>학과와 신분을 고르면 접속할 클러스터를 자동으로 정합니다. 비밀번호는 저장하지 않습니다.</p>
    {mode === "ssh" && <>
      <input autoComplete="username" placeholder="SERAPH 사용자명" value={username} onChange={(e) => setUsername(e.target.value)}/>
      <select value={major} onChange={(e) => setMajor(e.target.value)} aria-label="학과">
        <option value="">학과 선택</option>
        {(routing?.majors || []).map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
      </select>
      <div className="seg-row" role="group" aria-label="신분">
        {positions.map((p) => <button key={p.key} type="button" className={position === p.key ? "on" : ""} onClick={() => setPosition(p.key)}>{p.label}</button>)}
      </div>
      <div className="seg-row" role="group" aria-label="접속 위치">
        <button type="button" className={offCampus ? "" : "on"} onClick={() => setOffCampus(false)}>교내 · {ports.on_campus}</button>
        <button type="button" className={offCampus ? "on" : ""} onClick={() => setOffCampus(true)}>교외 · {ports.off_campus}</button>
      </div>
      <div className={`connect-target ${!manual && autoCluster ? "ok" : ""}`}>
        {manual ? "직접 지정한 주소로 접속합니다."
          : autoCluster ? <><strong>{autoCluster}</strong> 클러스터 · <code>{autoHost}:{autoPort}</code></>
          : "학과를 선택하면 접속할 클러스터가 정해집니다."}
      </div>
      {manual && <div className="connect-endpoint">
        <select value={host} onChange={(e) => setHost(e.target.value)} aria-label="호스트">
          <option value="">호스트 선택</option>
          {Object.values(clusters || {}).map((c) => <option key={c.host} value={c.host}>{c.name} · {c.host}</option>)}
        </select>
        <input type="number" min="1" max="65535" placeholder="포트" value={port} onChange={(e) => setPort(e.target.value)}/>
      </div>}
      <input type="password" autoComplete="off" placeholder="SSH 비밀번호 (키 인증이면 비워 두기)" value={password}
        onChange={(e) => setPassword(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()}/>
      <button type="button" className="link-toggle" onClick={() => setManual((v) => !v)}>
        {manual ? "← 학과로 자동 선택하기" : "호스트·포트 직접 지정"}
      </button>
    </>}
    <button className="primary full" onClick={submit} disabled={loading || !ready}>{loading ? "연결 중…" : "SERAPH 연결"}</button>
  </div></div>;
}

// 서랍 공통 껍데기. 바깥을 누르거나 Esc 로 닫힌다 — 닫는 방법이 X 버튼 하나뿐이면
// 사용자는 갇혔다고 느낀다. 내 작업·완료 이력 두 서랍이 같이 쓴다.
function DrawerShell({ onClose, label, children }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return <>
    <div className="drawer-scrim" onClick={onClose} aria-hidden="true"/>
    <div className="job-drawer" role="dialog" aria-modal="true" aria-label={label || "상세"}>{children}</div>
  </>;
}

function fmtBytes(n) {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)}${u[i]}`;
}

// NAS 탐색기. 데이터 경로를 눈으로 보고 고르게 한다 — 예전에는 서버 경로를
// 맨 입력창에 직접 타이핑해야 했고, 기본값조차 존재하지 않는 경로였다.
function NasBrowser({ open, onClose, onPick, onUpload, uploading }) {
  const [path, setPath] = useState(null);
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(async (target) => {
    setBusy(true); setErr(null);
    try {
      const q = target ? `?path=${encodeURIComponent(target)}` : "";
      const d = await api(`/api/v1/remote/ls${q}`);
      setData(d); setPath(d.path);
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }, []);

  useEffect(() => { if (open) load(path); }, [open]);   // 열 때만 로드
  if (!open) return null;

  const entries = data?.entries || [];
  return <div className="connect-overlay" onClick={onClose}>
    <div className="nas-modal" onClick={(e) => e.stopPropagation()}>
      <div className="nas-head">
        <div>
          <p className="eyebrow">NAS BROWSER</p>
          <h2>데이터 파일 선택</h2>
        </div>
        <button className="icon-button" onClick={onClose} aria-label="닫기"><Icon name="close" size={18}/></button>
      </div>

      <div className="nas-bar">
        <button className="secondary compact" disabled={!data?.parent || busy} onClick={() => load(data.parent)}>상위</button>
        <code className="nas-path">{path || "…"}</code>
        <button className="secondary compact" disabled={busy} onClick={() => load(data?.data_root)}>내 폴더</button>
      </div>

      {err && <p className="nas-err">{err}</p>}

      <div className="nas-list">
        {busy && <div className="empty-mini"><span>불러오는 중…</span></div>}
        {!busy && !entries.length && !err && <div className="empty-mini"><Icon name="folder" size={20}/><span>이 폴더는 비어 있습니다.</span></div>}
        {!busy && entries.map((e) => <button
          key={e.path}
          className={`nas-row ${e.is_dir ? "dir" : e.is_archive ? "ok" : "dim"}`}
          onClick={() => e.is_dir ? load(e.path) : e.is_archive && onPick(e.path)}
          disabled={!e.is_dir && !e.is_archive}
          title={e.is_dir ? "" : e.is_archive ? "이 파일을 데이터로 사용" : "압축 파일(.tar/.tar.gz/.tgz/.zip)만 사용할 수 있습니다"}>
          <Icon name={e.is_dir ? "folder" : "server"} size={15}/>
          <span className="nas-name">{e.name}</span>
          <span className="nas-size">{e.is_dir ? "폴더" : fmtBytes(e.size)}</span>
        </button>)}
      </div>

      <div className="nas-foot">
        <span>압축 파일(.tar · .tar.gz · .tgz · .zip)만 고를 수 있습니다 — NAS IOPS 보호</span>
        <button className="primary compact" onClick={onUpload} disabled={uploading}>
          <Icon name="plus" size={15}/> {uploading ? "올리는 중…" : "내 PC에서 올리기"}
        </button>
      </div>
    </div>
  </div>;
}

// 빌드는 20분까지 걸린다. 새로고침하거나 실수로 탭을 닫아도 진행 상황을 다시
// 찾을 수 있어야 한다 — 서버에서는 계속 돌고 있는데 화면만 잊어버리면 곤란하다.
const ENV_BUILD_KEY = "seraph_env_build";
const ENV_BUILD_POLL_MS = 3000;

function splitList(text) {
  return text.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
}

const ENV_SOURCE_LABELS = { personal: "내가 만든 환경", "personal-install": "내 설치", shared: "공용" };

const ENV_MODES = [
  { id: "clone", label: "기존 환경 복제", detail: "공용 환경의 torch 를 그대로 물려받고 패키지만 추가합니다. 몇 분." },
  { id: "scratch", label: "처음부터 만들기", detail: "파이썬·torch·CUDA 버전을 전부 고릅니다. 15분 이상 걸릴 수 있습니다." },
  { id: "venv", label: "venv (가장 빠름)", detail: "기존 파이썬 위에 얹습니다. 몇 초. 파이썬 버전은 바꿀 수 없습니다." },
];

// 환경 화면. 공용 설치는 읽기 전용이라 pip 하나 넣을 수 없었고, 원하는 torch
// 버전이 없는 순간 학생은 이 도구를 닫고 터미널을 열었다. 그 마지막 구멍을 막는다.
function EnvsPage({ report, onEnvsChanged }) {
  const [tools, setTools] = useState(null);
  const [envs, setEnvs] = useState([]);
  const [build, setBuild] = useState(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    mode: "clone", name: "", python: "3.11", source: "",
    condaText: "", pipText: "", channelsText: "conda-forge",
  });

  const loadEnvs = useCallback(async () => {
    try {
      const data = await api("/api/v1/envs");
      setEnvs(data.envs || []);
      setForm((old) => old.source ? old : { ...old, source: (data.envs || [])[0]?.name || "" });
      onEnvsChanged?.(data.envs || []);
      // 서버가 아직 돌리고 있는 빌드가 있으면 이어서 보여준다. 브라우저에 저장된
      // 번호만 믿으면, 다른 브라우저로 열거나 저장소를 지웠을 때 20분짜리 빌드가
      // 화면에서 통째로 사라진다.
      const running = (data.running_builds || [])[0];
      if (running) {
        setBuild((old) => old?.build_id ? old : { ...running, state: "running", log: "" });
      }
    } catch (err) { report(err); }
  }, [report, onEnvsChanged]);

  useEffect(() => {
    loadEnvs();
    // 점검은 df·curl 을 돌리므로 몇 초 걸린다. 목록과 따로 부른다.
    api("/api/v1/envs/tools").then(setTools).catch(report);
    try {
      const saved = localStorage.getItem(ENV_BUILD_KEY);
      if (saved) setBuild({ build_id: saved, state: "running", log: "" });
    } catch { /* 저장소를 못 읽어도 화면은 떠야 한다 */ }
  }, [loadEnvs, report]);

  // 진행 중인 빌드만 폴링한다. 끝나면 목록을 다시 불러 새 환경이 바로 보이게 한다.
  useEffect(() => {
    if (!build?.build_id || build.state !== "running") return undefined;
    let stop = false;
    const tick = async () => {
      try {
        const data = await api(`/api/v1/envs/builds/${build.build_id}`);
        if (stop) return;
        setBuild(data.build);
        if (data.build.state !== "running") {
          try { localStorage.removeItem(ENV_BUILD_KEY); } catch { /* 무시 */ }
          loadEnvs();
        }
      } catch (err) {
        if (stop) return;
        // 백엔드 재시작으로 기록을 못 찾으면 계속 물어봐야 소용없다.
        setBuild((old) => ({ ...(old || {}), state: "failed", message: err.message }));
        try { localStorage.removeItem(ENV_BUILD_KEY); } catch { /* 무시 */ }
      }
    };
    const timer = setInterval(tick, ENV_BUILD_POLL_MS);
    tick();
    return () => { stop = true; clearInterval(timer); };
  }, [build?.build_id, build?.state, loadEnvs]);

  const create = async () => {
    setBusy(true);
    try {
      const payload = {
        name: form.name.trim(),
        mode: form.mode,
        python: form.python,
        source: form.mode === "scratch" ? null : (form.source || null),
        conda_packages: form.mode === "scratch" ? splitList(form.condaText) : [],
        pip_packages: splitList(form.pipText),
        channels: form.mode === "scratch" ? splitList(form.channelsText) : ["conda-forge"],
      };
      const data = await api("/api/v1/envs", { method: "POST", body: JSON.stringify(payload) });
      setBuild(data.build);
      try { localStorage.setItem(ENV_BUILD_KEY, data.build.build_id); } catch { /* 무시 */ }
    } catch (err) { report(err); }
    finally { setBusy(false); }
  };

  const remove = async (name) => {
    if (!window.confirm(`'${name}' 환경을 지웁니다. 이 환경을 쓰는 작업은 더 이상 실행되지 않습니다.`)) return;
    setBusy(true);
    try { await api(`/api/v1/envs/${encodeURIComponent(name)}`, { method: "DELETE" }); await loadEnvs(); }
    catch (err) { report(err); }
    finally { setBusy(false); }
  };

  const applyPreset = (preset) => setForm((old) => ({
    ...old, mode: "scratch", python: preset.python,
    condaText: preset.conda_packages.join(" "), channelsText: preset.channels.join(" "),
  }));

  const mine = envs.filter((e) => e.source === "personal");
  const others = envs.filter((e) => e.source !== "personal");
  const running = build?.state === "running";
  const nameTaken = envs.some((e) => e.name === form.name.trim());
  const canSubmit = !busy && !running && form.name.trim() && !nameTaken
    && (form.mode === "scratch" || form.source) && tools?.can_create !== false;

  return <section className="page envs-page">
    <EnvReadiness tools={tools}/>

    <div className="envs-grid">
      <article className="panel">
        <div className="panel-head">
          <div><p className="eyebrow">MY ENVIRONMENTS</p><h2>내 환경</h2></div>
          <button className="secondary compact" onClick={loadEnvs} disabled={busy}><Icon name="refresh" size={16}/> 새로고침</button>
        </div>
        {mine.length ? <ul className="env-list">{mine.map((env) => <li key={env.name}>
          <div><strong>{env.name}</strong><small>{env.python || env.kind} · {env.prefix}</small></div>
          <button className="icon-button" onClick={() => remove(env.name)} disabled={busy} title="이 환경 지우기"><Icon name="trash" size={16}/></button>
        </li>)}</ul>
          : <div className="empty-mini"><Icon name="box" size={20}/><span>아직 직접 만든 환경이 없습니다.</span></div>}

        <p className="eyebrow env-shared-head">읽기 전용 · 공용/기존 설치</p>
        {others.length ? <ul className="env-list muted-list">{others.map((env) => <li key={env.name}>
          <div><strong>{env.name}</strong><small>{env.prefix}</small></div>
          <span className="env-tag">{env.source === "shared" ? "공용" : "내 설치"}</span>
        </li>)}</ul>
          : <div className="empty-mini"><span>공용 환경을 찾지 못했습니다.</span></div>}
      </article>

      <article className="panel">
        <div className="panel-head"><div><p className="eyebrow">NEW ENVIRONMENT</p><h2>새 환경 만들기</h2></div></div>

        <div className="env-modes">{ENV_MODES.map((mode) => <button
          key={mode.id}
          className={`env-mode ${form.mode === mode.id ? "active" : ""}`}
          onClick={() => setForm({ ...form, mode: mode.id })}
          disabled={running}>
          <strong>{mode.label}</strong><span>{mode.detail}</span>
        </button>)}</div>

        <Field label="환경 이름" hint={nameTaken ? "같은 이름이 이미 있습니다." : "작업 만들 때 이 이름으로 고릅니다"}>
          <input value={form.name} placeholder="예: torch25" disabled={running}
            onChange={(e) => setForm({ ...form, name: e.target.value })}/>
        </Field>

        {form.mode === "scratch" ? <>
          <Field label="파이썬 버전">
            <select value={form.python} disabled={running} onChange={(e) => setForm({ ...form, python: e.target.value })}>
              {(tools?.python_versions || ["3.11"]).map((v) => <option key={v}>{v}</option>)}
            </select>
          </Field>
          {tools?.presets?.length ? <div className="env-presets">
            <p className="eyebrow">자주 쓰는 조합</p>
            <div className="env-preset-row">{tools.presets.map((preset) => <button key={preset.id}
              className="secondary compact" disabled={running} onClick={() => applyPreset(preset)}
              title={preset.note}>{preset.label}</button>)}</div>
          </div> : null}
          <Field label="conda 패키지" hint="공백으로 구분 · 예: pytorch torchvision pytorch-cuda=12.1">
            <input value={form.condaText} disabled={running}
              onChange={(e) => setForm({ ...form, condaText: e.target.value })}/>
          </Field>
          <Field label="채널" hint="pytorch·nvidia 채널이 있어야 CUDA 빌드를 받습니다">
            <input value={form.channelsText} disabled={running}
              onChange={(e) => setForm({ ...form, channelsText: e.target.value })}/>
          </Field>
        </> : <Field label={form.mode === "clone" ? "복제할 환경" : "기반 파이썬"}
          hint={form.mode === "clone" ? "이 환경의 패키지를 그대로 복사합니다" : "이 환경의 파이썬으로 venv 를 만듭니다"}>
          <select value={form.source} disabled={running} onChange={(e) => setForm({ ...form, source: e.target.value })}>
            <option value="">환경을 고르세요</option>
            {envs.map((env) => <option key={env.name} value={env.name}>{env.name}</option>)}
          </select>
        </Field>}

        <Field label="pip 패키지 (선택)" hint="공백으로 구분 · 예: wandb timm==1.0.9">
          <input value={form.pipText} disabled={running}
            onChange={(e) => setForm({ ...form, pipText: e.target.value })}/>
        </Field>

        <button className="primary full" onClick={create} disabled={!canSubmit}>
          {running ? "만드는 중…" : "이 환경 만들기"} <Icon name="arrow" size={17}/>
        </button>
        <p className="muted env-note">
          서버에서 백그라운드로 만듭니다. 창을 닫아도 계속되고, 다시 열면 진행 상황이 이어집니다.
        </p>
      </article>
    </div>

    {build && <article className="panel env-build-panel">
      <div className="panel-head">
        <div><p className="eyebrow">BUILD LOG</p><h2>{build.name || "환경 만들기"}</h2></div>
        <StatusPill status={{ running: "RUNNING", succeeded: "COMPLETED", failed: "FAILED" }[build.state] || "PENDING"}/>
      </div>
      {build.message && <p className="muted">{build.message}</p>}
      <pre className="logs">{build.log || "로그를 기다리는 중입니다…"}</pre>
      {build.state !== "running" && <button className="secondary compact" onClick={() => setBuild(null)}>닫기</button>}
    </article>}
  </section>;
}

// 점검 결과. "가능/불가"를 먼저 말하고 근거를 보여준다 — 추측으로 20분을 태우지
// 않게 하려고 만든 화면이라, 근거를 감추면 존재 이유가 없어진다.
function EnvReadiness({ tools }) {
  if (!tools) return <article className="panel env-probe"><p className="muted">서버에서 환경 만들기 가능 여부를 확인하는 중입니다…</p></article>;
  const gib = tools.avail_bytes ? `${(tools.avail_bytes / 1024 ** 3).toFixed(0)}GB` : "확인 불가";
  return <article className={`panel env-probe ${tools.can_create ? "ok" : "bad"}`}>
    <div className="panel-head">
      <div><p className="eyebrow">READINESS</p><h2>{tools.can_create ? "이 서버에서 환경을 만들 수 있습니다" : "지금은 환경을 만들 수 없습니다"}</h2></div>
      <Icon name={tools.can_create ? "check" : "warn"} size={22}/>
    </div>
    {tools.blockers?.map((text) => <p key={text} className="env-blocker"><Icon name="warn" size={15}/> {text}</p>)}
    {tools.warnings?.map((text) => <p key={text} className="env-warning"><Icon name="warn" size={15}/> {text}</p>)}
    <dl className="detail-grid env-facts">
      <div><dt>conda</dt><dd>{tools.conda_version || "찾지 못함"}</dd></div>
      <div><dt>만들 위치</dt><dd>{tools.envs_root}</dd></div>
      <div><dt>{tools.filesystem || "파일시스템"} 여유</dt><dd>{gib}</dd></div>
      <div><dt>로그인 노드 부하</dt><dd>{tools.loadavg || "—"}{tools.cpus ? ` · ${tools.cpus} CPU` : ""}</dd></div>
      {(tools.network || []).map((net) => <div key={net.url}>
        <dt>{net.url.includes("pypi") ? "PyPI" : "conda 저장소"}</dt>
        <dd className={net.ok ? "net-ok" : "net-bad"}>{net.ok ? `연결됨 (${net.status})` : "닿지 않음"}</dd>
      </div>)}
    </dl>
  </article>;
}

// 알림. 백엔드가 서버를 지켜보다 끝난 일을 알려주면, 화면은 그걸 브라우저 알림과
// 인앱 배너로 보여준다. 폴링을 pageVisible 로 막지 않는 이유는 명확하다 —
// 탭이 가려져 있을 때가 바로 알림이 필요한 순간이다.
const EVENTS_POLL_MS = 15_000;

function canNotify() {
  return typeof Notification !== "undefined" && Notification.permission === "granted";
}

function AlertBell({ state, onEnable }) {
  if (state === "unsupported") return null;
  if (state === "granted") {
    return <div className="alert-bell on" title="작업·환경이 끝나면 알림을 보냅니다"><Icon name="bell" size={18}/></div>;
  }
  const denied = state === "denied";
  return <button className="alert-bell" onClick={onEnable} disabled={denied}
    title={denied ? "브라우저에서 이 사이트의 알림을 차단했습니다. 주소창 왼쪽 자물쇠에서 허용으로 바꿔주세요." : "작업이 끝나면 알려드립니다"}>
    <Icon name="bell" size={18}/><span>{denied ? "알림 차단됨" : "알림 켜기"}</span>
  </button>;
}

// 브라우저 알림 권한이 없어도 무언가는 보여야 한다. 권한이 있으면 OS 알림이
// 뜨고, 이 배너는 화면을 보고 있던 사람을 위한 것이다.
function AlertBanner({ alerts, onDismiss }) {
  if (!alerts.length) return null;
  return <div className="alert-stack">{alerts.map((alert) => <div key={alert.id}
    className={`alert-card ${alert.ok ? "ok" : "bad"}`} role="status">
    <Icon name={alert.ok ? "check" : "warn"} size={18}/>
    <div><strong>{alert.title}</strong><p>{alert.body}</p></div>
    <button onClick={() => onDismiss(alert.id)} aria-label="닫기"><Icon name="close" size={16}/></button>
  </div>)}</div>;
}

function ErrorToast({ error, onClose }) {
  if (!error) return null;
  return <div className="toast" role="alert">
    <div><strong>{error.code || "오류"}</strong><p>{error.message}</p></div>
    <button onClick={onClose} aria-label="닫기"><Icon name="close" size={18}/></button>
  </div>;
}

// 노드 표 정렬. 기본은 이름순이고, '사용 가능순'은 빠른 실행 추천과 같은 관점(여유 많은 노드 우선)이다.
const NODE_SORT_KEY = "seraph_node_sort";
const NODE_SORTS = {
  name:  { label: "이름순", cmp: null },
  free:  { label: "사용 가능 GPU 많은 순", cmp: (a, b) => (b.usable_gpus ?? 0) - (a.usable_gpus ?? 0) },
  total: { label: "전체 GPU 많은 순",      cmp: (a, b) => (b.total_gpus ?? 0) - (a.total_gpus ?? 0) },
};

// 탭마다 [작은 영문 라벨, 제목]. 삼항 연산자를 일곱 번 잇는 것보다 한 줄 더하기 쉽다.
const PAGE_TITLES = {
  dashboard: ["CLUSTER OVERVIEW", "클러스터 대시보드"],
  new: ["JOB WIZARD", "새 GPU 작업"],
  jobs: ["JOB MONITOR", "내 작업"],
  envs: ["ENVIRONMENTS", "파이썬 환경"],
  history: ["JOB HISTORY", "완료 작업 이력"],
  tutorial: ["GUIDE", "세라프 사용법"],
  notices: ["ANNOUNCEMENTS", "공지사항"],
};

const blankForm = {
  name: "image-train", local_code_path: "", entrypoint: "train.py", argsText: "--data\n{dataset}\n--output\n{output}",
  dataset_path: "/data/datasets/tarfiles/images.tar.gz", output_path: "/data/사용자명/results/image-train",
  copy_dataset_to_local: true, partition: "", gpus: 1, high_perf: false, cpus: 8, memory: "32G",
  time_limit: "02:00:00", node: "", conda_env: "",
};

export default function App() {
  const [tab, setTab] = useState("dashboard");
  const [health, setHealth] = useState(null);
  const [me, setMe] = useState(null);
  const [cluster, setCluster] = useState(null);
  const [usage, setUsage] = useState(null);
  const [nodes, setNodes] = useState([]);
  const [partitions, setPartitions] = useState({});
  const [diagnosis, setDiagnosis] = useState(null);
  const [queue, setQueue] = useState(null);
  const [history, setHistory] = useState([]);
  const [clusterInfo, setClusterInfo] = useState(null);
  const [jobHistory, setJobHistory] = useState(null);
  const [historyDays, setHistoryDays] = useState(7);
  const [historyJob, setHistoryJob] = useState(null);
  const [tutorial, setTutorial] = useState(null);
  const [tutMode, setTutMode] = useState("practice");
  const [announcements, setAnnouncements] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [form, setForm] = useState(blankForm);
  const [recommendation, setRecommendation] = useState(null);
  const [validation, setValidation] = useState(null);
  const [prepared, setPrepared] = useState(null);
  const [confirmSubmit, setConfirmSubmit] = useState(false);
  const [selected, setSelected] = useState(null);
  const [logs, setLogs] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  // 접속 폼 상태(사용자명·학과·비밀번호)는 ConnectCard 안에 있다.
  // 연결에 성공하면 카드가 언마운트되면서 비밀번호도 함께 사라진다.

  // NAS 데이터 선택·업로드. 서버 경로를 타이핑하지 않아도 되게 한다.
  const [nasOpen, setNasOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  // 작업 폼의 환경 드롭다운. '환경' 화면에서 새로 만들면 여기로도 바로 올라온다.
  const [envOptions, setEnvOptions] = useState([]);

  // 노드 표 정렬 기준. 한 번 고르면 다음에 들어와도 그대로 쓰도록 저장한다.
  const [nodeSort, setNodeSort] = useState(() => {
    try { return localStorage.getItem(NODE_SORT_KEY) || "name"; } catch { return "name"; }
  });
  useEffect(() => { try { localStorage.setItem(NODE_SORT_KEY, nodeSort); } catch { /* 저장 실패는 무시 */ } }, [nodeSort]);
  const [pageVisible, setPageVisible] = useState(() => document.visibilityState === "visible");

  const report = useCallback((err) => setError({ code: err.code, message: err.message }), []);

  // --- 알림 -----------------------------------------------------------------
  const [alerts, setAlerts] = useState([]);
  const [notifyState, setNotifyState] = useState(
    () => typeof Notification === "undefined" ? "unsupported" : Notification.permission);
  // 커서는 ref 다. 폴링 타이머를 다시 만들지 않으면서 마지막으로 본 번호를 기억해야 한다.
  const eventCursor = useRef(null);

  const enableNotifications = useCallback(async () => {
    if (typeof Notification === "undefined") return;
    try { setNotifyState(await Notification.requestPermission()); }
    catch { setNotifyState(Notification.permission); }
  }, []);

  const dismissAlert = useCallback(
    (id) => setAlerts((items) => items.filter((item) => item.id !== id)), []);

  useEffect(() => {
    if (!health?.seraph_reachable) return undefined;
    let stopped = false;
    const tick = async () => {
      const cursor = eventCursor.current;
      const query = new URLSearchParams({ can_notify: String(canNotify()) });
      if (cursor) { query.set("since", cursor.id); query.set("session", cursor.session); }
      let data;
      // 알림 폴링이 실패했다고 화면에 오류를 띄우지는 않는다. 이건 배경 작업이고,
      // 사용자가 지금 하려던 일과 아무 상관이 없다.
      try { data = await api(`/api/v1/events?${query}`, { timeoutMs: 10_000 }); }
      catch { return; }
      if (stopped) return;
      // 첫 폴링은 기준점만 잡는다. 브라우저가 꺼져 있는 동안 쌓인 것까지 쏟아내면
      // 이미 OS 알림으로 받은 걸 또 받게 된다.
      const baseline = !cursor || data.reset;
      eventCursor.current = { session: data.session, id: data.latest_id };
      if (baseline || !data.events?.length) return;
      setAlerts((items) => [...data.events, ...items].slice(0, 5));
      if (canNotify()) {
        for (const event of data.events) {
          try { new Notification(event.title, { body: event.body, tag: `seraph-${event.id}` }); }
          catch { /* 알림을 못 띄워도 인앱 배너는 남는다 */ }
        }
      }
    };
    tick();
    const timer = window.setInterval(tick, EVENTS_POLL_MS);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [health?.seraph_reachable]);

  const loadDashboard = useCallback(async () => {
    try {
      const [meData, statusData, usageData, nodeData, partitionData, diagnosisData, queueData, historyData] = await Promise.all([
        api("/api/v1/me"), api("/api/v1/cluster/status"), api("/api/v1/cluster/usage"),
        api("/api/v1/cluster/nodes?gpus=1"), api("/api/v1/cluster/partitions"), api("/api/v1/jobs/diagnosis"),
        api("/api/v1/queue"), api("/api/v1/cluster/history"),
      ]);
      setMe(meData); setCluster(statusData); setUsage(usageData); setNodes(nodeData.nodes || []);
      setPartitions(partitionData.partitions || {}); setDiagnosis(diagnosisData);
      setQueue(queueData); setHistory(historyData.samples || []);
      setForm((old) => ({
        ...old,
        partition: old.partition || meData.default_partition || "",
        output_path: old.output_path === blankForm.output_path ? `/data/${meData.user}/results/${old.name}` : old.output_path,
      }));
    } catch (err) { report(err); }
  }, [report]);

  const loadJobs = useCallback(async () => {
    try { const data = await api("/api/v1/jobs"); setJobs(data.jobs || []); }
    catch (err) { report(err); }
  }, [report]);

  const loadHistory = useCallback(async (days) => {
    // sacct 는 느리고 자주 바뀌지 않으므로 사용자가 요청할 때만 부른다(자동 폴링 없음).
    setLoading(true); setError(null); setHistoryDays(days); setHistoryJob(null);
    try { setJobHistory(await api(`/api/v1/jobs/history?days=${days}&limit=50`)); }
    catch (err) { report(err); }
    finally { setLoading(false); }
  }, [report]);

  const initialize = useCallback(async () => {
    try {
      const data = await api("/api/v1/health"); setHealth(data);
      api("/api/v1/clusters").then(setClusterInfo).catch(() => {});  // 정적 안내(라우팅 표 포함), 1회만
      if (data.seraph_reachable) {
        await Promise.all([loadDashboard(), loadJobs()]);
        // 환경 목록은 잘 안 바뀐다. 실패해도 폼은 그대로 쓸 수 있어야 한다.
        api("/api/v1/envs").then((c) => setEnvOptions(c.envs || [])).catch(() => {});
      }
    } catch (err) { report(err); }
  }, [loadDashboard, loadJobs, report]);

  useEffect(() => { initialize(); }, [initialize]);
  useEffect(() => {
    const onVisibilityChange = () => setPageVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, []);
  useEffect(() => {
    if (!health?.seraph_reachable || !pageVisible) return undefined;
    // SFTP 작업 폴더 목록은 자동 탐색하지 않는다. 대시보드 Snapshot만 갱신한다.
    const timer = window.setInterval(() => { loadDashboard(); }, DASHBOARD_POLL_MS);
    return () => window.clearInterval(timer);
  }, [health?.seraph_reachable, pageVisible, loadDashboard]);
  useEffect(() => {
    if (tab === "history" && health?.seraph_reachable && !jobHistory) loadHistory(historyDays);
  }, [tab, health?.seraph_reachable, jobHistory, historyDays, loadHistory]);
  useEffect(() => {
    if (tab === "tutorial" && !tutorial) api("/api/v1/tutorial").then(setTutorial).catch(report);
  }, [tab, tutorial, report]);

  const loadAnnouncements = useCallback(async () => {
    try { setAnnouncements(await api("/api/v1/announcements")); } catch (err) { report(err); }
  }, [report]);
  useEffect(() => {
    if (tab === "notices" && !announcements) loadAnnouncements();
  }, [tab, announcements, loadAnnouncements]);

  const refreshJobStatus = useCallback(async (localId) => {
    try {
      const detail = await api(`/api/v1/jobs/${localId}`);
      setSelected(detail);
      setJobs((items) => items.map((item) => item.local_job_id === localId ? detail.job : item));
    } catch (err) { report(err); }
  }, [report]);

  const refreshJobLogs = useCallback(async (localId) => {
    try { setLogs(await api(`/api/v1/jobs/${localId}/logs`)); }
    catch (err) { report(err); }
  }, [report]);

  const openJob = useCallback(async (localId) => {
    try {
      const [detail, logData] = await Promise.all([
        api(`/api/v1/jobs/${localId}`), api(`/api/v1/jobs/${localId}/logs`),
      ]);
      setSelected(detail); setLogs(logData);
      setJobs((items) => items.map((item) => item.local_job_id === localId ? detail.job : item));
    } catch (err) { report(err); }
  }, [report]);

  useEffect(() => {
    const id = selected?.job?.local_job_id;
    if (!id || !pageVisible || !ACTIVE.has(selected.job.status)) return undefined;
    // 실행 상태만 낮은 빈도로 확인한다. 로그 SFTP 읽기는 사용자가 요청할 때만 한다.
    const timer = window.setInterval(() => refreshJobStatus(id), ACTIVE_JOB_POLL_MS);
    return () => window.clearInterval(timer);
  }, [selected?.job?.local_job_id, selected?.job?.status, pageVisible, refreshJobStatus]);

  const payload = useMemo(() => ({
    name: form.name, local_code_path: form.local_code_path, entrypoint: form.entrypoint,
    arguments: form.argsText.split("\n").map((item) => item.trim()).filter(Boolean),
    dataset_path: form.dataset_path, output_path: form.output_path,
    copy_dataset_to_local: form.copy_dataset_to_local, partition: form.partition || null,
    gpus: Number(form.gpus), high_perf: form.high_perf, cpus: Number(form.cpus), memory: form.memory,
    time_limit: form.time_limit, node: form.node || null, conda_env: form.conda_env || null,
  }), [form]);

  const runAction = async (action) => {
    setLoading(true); setError(null);
    try { await action(); } catch (err) { report(err); } finally { setLoading(false); }
  };

  const chooseCode = (kind) => runAction(async () => {
    const data = await api(`/api/v1/local/select-code?kind=${kind}`, { method: "POST" });
    if (data.path) setForm((old) => ({ ...old, local_code_path: data.path }));
  });

  const pickDataset = (path) => { setForm((old) => ({ ...old, dataset_path: path })); setNasOpen(false); };

  const uploadDataset = () => runAction(async () => {
    setUploading(true);
    try {
      const d = await api("/api/v1/remote/datasets/upload", { method: "POST" });
      if (d.selected && d.dataset) { pickDataset(d.dataset.path); }
    } finally { setUploading(false); }
  });

  const recommend = () => runAction(async () => {
    const [hours, minutes] = form.time_limit.split(":").map(Number);
    const data = await api("/api/v1/recommendations", {
      method: "POST", body: JSON.stringify({ gpus: Number(form.gpus), hours: hours + minutes / 60, high_perf: form.high_perf, node: null }),
    });
    setRecommendation(data);
    if (data.best) setForm((old) => ({ ...old, partition: data.best.partition, node: data.best.node || "" }));
  });

  const validate = () => runAction(async () => {
    const data = await api("/api/v1/jobs/validate", { method: "POST", body: JSON.stringify(payload) });
    setValidation(data); setPrepared(null);
  });

  const prepare = () => runAction(async () => {
    const checked = await api("/api/v1/jobs/validate", { method: "POST", body: JSON.stringify(payload) });
    setValidation(checked);
    if (!checked.ok) return;
    const data = await api("/api/v1/jobs/prepare", { method: "POST", body: JSON.stringify(payload) });
    setPrepared(data); setConfirmSubmit(false); await loadJobs();
  });

  const submit = () => runAction(async () => {
    if (!confirmSubmit || !prepared) return;
    const requestId = globalThis.crypto?.randomUUID?.() || `request-${Date.now()}`;
    const data = await api(`/api/v1/jobs/${prepared.job.local_job_id}/submit`, {
      method: "POST", body: JSON.stringify({ request_id: requestId, confirmed: true }),
    });
    setPrepared((old) => ({ ...old, job: data.job })); await loadJobs(); await openJob(data.job.local_job_id); setTab("jobs");
  });

  const preflight = () => runAction(async () => {
    if (!prepared) return;
    const data = await api(`/api/v1/jobs/${prepared.job.local_job_id}/preflight`, { method: "POST" });
    setPrepared((old) => ({ ...old, job: data.job, preflight: data.preflight }));
  });

  const cancel = (localId) => runAction(async () => {
    if (!window.confirm("이 Slurm 작업을 취소할까요?")) return;
    await api(`/api/v1/jobs/${localId}/cancel`, { method: "POST" }); await openJob(localId); await loadJobs();
  });

  // 끝난 작업을 목록에서 치운다. 결과물과 데이터셋은 작업 폴더 밖이라 남는다.
  const removeJob = (localId) => runAction(async () => {
    if (!window.confirm("이 작업 기록을 지울까요?\n업로드한 코드와 로그가 함께 삭제됩니다.\n(결과물과 데이터셋은 그대로 남습니다)")) return;
    await api(`/api/v1/jobs/${localId}`, { method: "DELETE" });
    setSelected(null); setLogs(null); await loadJobs();
  });

  const connect = ({ username, host, port, password }) => runAction(async () => {
    await api("/api/v1/session/connect", {
      method: "POST",
      body: JSON.stringify({
        username: username || null,
        host: host || null,
        port: port ? Number(port) : null,
        password: password || null,
      }),
    });
    await initialize();
  });

  const refreshAll = () => runAction(async () => {
    await api("/api/v1/cluster/refresh", { method: "POST" }); await Promise.all([loadDashboard(), loadJobs()]);
  });

  const disconnect = () => runAction(async () => {
    if (!window.confirm("SERAPH 연결을 해제할까요? (다시 접속하려면 비밀번호를 입력해야 합니다)")) return;
    await api("/api/v1/session/disconnect", { method: "POST" });
    setSelected(null); setMe(null); setCluster(null); setQueue(null);
    setJobHistory(null); setTutorial(null); setAnnouncements(null);
    await initialize();   // health 재조회 -> 미연결 -> 연결 화면 표시
  });

  const nav = [
    ["dashboard", "grid", "대시보드"], ["new", "plus", "새 작업"], ["jobs", "jobs", "내 작업"], ["envs", "box", "환경"], ["history", "history", "완료 이력"], ["tutorial", "book", "튜토리얼"], ["notices", "bell", "공지"],
  ];
  // 사이드바 배지는 "지금 신경 쓸 게 있나"를 뜻한다. 전체 개수를 띄우면 끝난 작업이
  // 남아 있는 한 영원히 사라지지 않아서, 배지가 아무 의미도 갖지 못한다.
  const activeJobCount = useMemo(
    () => jobs.filter((j) => ACTIVE.has(j.status)).length, [jobs]);

  // 정렬한 뒤에 자르므로, '사용 가능순'이면 여유 많은 노드 8개가 위로 온다(-w 로 고를 노드 찾기).
  const visibleNodes = useMemo(() => {
    const list = [...(cluster?.nodes?.length ? cluster.nodes : nodes)];
    const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true });
    const cmp = NODE_SORTS[nodeSort]?.cmp;
    list.sort(cmp ? (a, b) => cmp(a, b) || byName(a, b) : byName);
    return list.slice(0, 8);
  }, [cluster, nodes, nodeSort]);

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><span /><span /><span /></div><div><strong>SERAPH</strong><small>GPU CONSOLE</small></div></div>
      <nav>{nav.map(([key, icon, label]) => <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}><Icon name={icon}/><span>{label}</span>{key === "jobs" && activeJobCount > 0 && <b>{activeJobCount}</b>}</button>)}</nav>
      <div className="side-note"><Icon name="server"/><div><strong>{health?.mode === "ssh" ? "SERAPH 연결" : "Mock 시연 모드"}</strong><span>{health?.seraph_reachable ? "정상 연결됨" : "연결 필요"}</span></div><i className={health?.seraph_reachable ? "online" : ""}/></div>
      <p className="version">Local console · v1.1.1</p>
    </aside>

    <main>
      <header>
        <div><p className="eyebrow">{(PAGE_TITLES[tab] || PAGE_TITLES.jobs)[0]}</p><h1>{(PAGE_TITLES[tab] || PAGE_TITLES.jobs)[1]}</h1></div>
        <div className="header-actions"><AlertBell state={notifyState} onEnable={enableNotifications}/><div className="user-chip"><span>{(me?.user || "U").slice(0, 1).toUpperCase()}</span><div><strong>{me?.user || "연결 대기"}</strong><small>{me?.account || health?.mode || "local"}</small></div></div><button className="icon-button" onClick={refreshAll} disabled={loading} title="새로고침"><Icon name="refresh"/></button>{health?.seraph_reachable && health?.mode === "ssh" && <button className="icon-button" onClick={disconnect} disabled={loading} title="로그아웃 (연결 해제)"><Icon name="logout"/></button>}</div>
      </header>

      {tab === "dashboard" && <section className="page dashboard-page">
        <div className="welcome-strip"><div><span className="live-dot"/>LIVE · {cluster?.partition || me?.default_partition || "SERAPH"}</div><p>{diagnosis?.headline || "GPU 현황과 내 작업 상태를 불러오는 중입니다."}</p><button onClick={() => setTab("new")}>새 작업 만들기 <Icon name="arrow" size={16}/></button></div>
        <div className="metrics">
          <Metric icon="gpu" label="사용 가능한 GPU" value={cluster?.free_gpus} detail={`전체 ${cluster?.total_gpus ?? "—"}개`} accent="mint"/>
          <Metric icon="server" label="GPU 사용률" value={cluster ? `${Math.round(cluster.utilization * 100)}%` : null} detail={`${cluster?.used_gpus ?? "—"}개 사용 중`} accent="blue"/>
          <Metric icon="jobs" label="실행 중인 작업" value={cluster?.running_jobs} detail={`대기 ${cluster?.pending_jobs ?? "—"}개`} accent="violet"/>
          <Metric icon="spark" label="내 GPU 사용" value={usage ? `${usage.gpus_in_use}/${usage.gpus_limit ?? "∞"}` : null} detail={`${usage?.pending_jobs ?? "—"}개 대기`} accent="amber"/>
        </div>
        <QuotaPanel usage={usage} me={me}/>
        <article className="panel trend-panel">
          <div className="panel-head"><div><p className="eyebrow">OCCUPANCY TREND</p><h2>GPU 점유 추세</h2></div><span>{history.length}개 표본 · 폴링마다 기록</span></div>
          <TrendChart samples={history}/>
        </article>
        <div className="dashboard-grid">
          <article className="panel resource-panel"><div className="panel-head"><div><p className="eyebrow">RESOURCE MAP</p><h2>노드 가용 현황</h2></div><select className="sort-select" value={nodeSort} onChange={(e) => setNodeSort(e.target.value)} aria-label="노드 정렬 기준">{Object.entries(NODE_SORTS).map(([key, s]) => <option key={key} value={key}>{s.label}</option>)}</select></div>
            <div className="node-table"><div className="node-row node-header"><span>노드</span><span>유형</span><span>상태</span><span>사용 가능</span><span>GPU</span></div>{visibleNodes.map((node) => <div className="node-row" key={node.name}><strong>{node.name}</strong><span>{node.is_high_perf ? "고성능" : "일반"}</span><span className={node.schedulable ? "node-ok" : "node-off"}>{node.schedulable ? "사용 가능" : node.state}</span><span>{node.usable_gpus} / {node.total_gpus}</span><div className="mini-bar"><i style={{ width: `${node.total_gpus ? node.usable_gpus / node.total_gpus * 100 : 0}%` }}/></div></div>)}</div>
          </article>
          <article className="panel quick-panel"><div className="panel-head"><div><p className="eyebrow">QUICK START</p><h2>빠른 실행 추천</h2></div><div className="spark-badge"><Icon name="spark" size={17}/></div></div>
            <p className="muted">Slurm의 실제 스케줄러에 물어보고 가장 빠른 파티션과 노드를 찾습니다.</p>
            <div className="quick-controls"><label>GPU 수<select value={form.gpus} onChange={(e) => setForm({...form, gpus: e.target.value})}>{[1,2,4,8,16].map(n => <option key={n}>{n}</option>)}</select></label><label>예상 시간<input value={form.time_limit} onChange={(e) => setForm({...form, time_limit: e.target.value})}/></label></div>
            <button className="primary full" onClick={recommend} disabled={loading}><Icon name="spark" size={18}/> 가장 빠른 위치 찾기</button>
            {recommendation && <div className={`recommend-box ${recommendation.can_start_now ? "now" : "wait"}`}><span>{recommendation.can_start_now ? "지금 실행 가능" : "추천 위치"}</span><strong>{recommendation.best ? `${recommendation.best.partition} · ${recommendation.best.node}` : "조건에 맞는 위치 없음"}</strong><p>{recommendation.headline}</p></div>}
          </article>
        </div>
        <div className="dashboard-grid queue-grid">
          <article className="panel eta-panel">
            <div className="panel-head"><div><p className="eyebrow">MY QUEUE</p><h2>내 대기 작업 · 예상 시작</h2></div>{queue?.my_next_position != null && <span className="pos-badge">대기열 {queue.my_next_position}번째</span>}</div>
            {diagnosis?.jobs?.length ? <ul className="eta-list">{diagnosis.jobs.map((job) => <EtaItem key={job.job_id} job={job}/>)}</ul>
              : <div className="empty-mini"><Icon name="check" size={20}/><span>대기 중인 내 작업이 없습니다.</span></div>}
          </article>
          <article className="panel queue-panel">
            <div className="panel-head"><div><p className="eyebrow">LIVE QUEUE</p><h2>실시간 대기열</h2></div><span>대기 {queue?.pending_count ?? "—"} · 실행 {queue?.running_count ?? "—"}</span></div>
            <QueueTable pending={queue?.pending || []}/>
          </article>
        </div>
        {clusterInfo && <article className="panel cluster-panel">
          <div className="panel-head"><div><p className="eyebrow">CLUSTERS</p><h2>클러스터 안내</h2></div><span>{clusterInfo.note}</span></div>
          {me?.on_primary === false && me?.cluster_notice && <div className="cluster-notice"><Icon name="warn" size={16}/><p>{me.cluster_notice}</p></div>}
          {/* '실시간'은 정적 표가 아니라 지금 붙어 있는 클러스터가 정한다(me.connected_cluster). */}
          <div className="cluster-cards">{Object.entries(clusterInfo.clusters).map(([name, c]) => <div className={`cluster-card ${me?.cluster === name ? "mine" : ""} ${me?.connected_cluster === name ? "" : "muted"}`} key={name}>
            <div className="cc-head"><strong>{name}</strong><span className="cc-tags">{me?.connected_cluster === name && <em className="cc-live">실시간</em>}{me?.cluster === name && <em className="cc-mine">내 소속</em>}</span></div>
            <div className="cc-gpu">{c.total_gpus}<span>GPU</span></div>
            <p className="cc-allowed">{c.allowed}</p>
            <p className="cc-host">{c.host}</p>
          </div>)}</div>
        </article>}
        <article className="panel recent-panel"><div className="panel-head"><div><p className="eyebrow">RECENT JOBS</p><h2>최근 작업</h2></div><button className="text-button" onClick={() => setTab("jobs")}>전체 보기 <Icon name="arrow" size={15}/></button></div><JobTable jobs={jobs.slice(0, 5)} onOpen={(id) => {openJob(id); setTab("jobs");}}/></article>
      </section>}

      {tab === "new" && <section className="page new-page">
        <div className="wizard-layout"><div className="form-column">
          <article className="panel form-panel"><SectionTitle number="01" title="코드와 실행" subtitle="제출 시점의 코드만 작업별 폴더에 한 번 업로드합니다."/>
            <div className="field-grid"><Field label="작업 이름"><input value={form.name} onChange={(e) => setForm({...form, name: e.target.value})}/></Field><Field label="진입 파일"><input value={form.entrypoint} onChange={(e) => setForm({...form, entrypoint: e.target.value})}/></Field></div>
            <Field label="로컬 코드 경로" hint="코드 폴더, .py, .zip, .tar.gz"><div className="path-input"><input placeholder="예: C:\Users\me\project" value={form.local_code_path} onChange={(e) => setForm({...form, local_code_path: e.target.value})}/><button onClick={() => chooseCode("directory")}><Icon name="folder" size={17}/> 폴더</button><button onClick={() => chooseCode("file")}>파일</button></div></Field>
            <Field label="실행 인자" hint="한 줄에 인자 하나 · {dataset}, {output} 사용 가능"><textarea rows="5" value={form.argsText} onChange={(e) => setForm({...form, argsText: e.target.value})}/></Field>
            <Field label="파이썬 환경 (선택)" hint={envOptions.length ? "원하는 버전이 없으면 '환경' 화면에서 직접 만들 수 있습니다" : "서버에서 환경을 찾지 못했습니다"}>
              {envOptions.length
                ? <select value={form.conda_env} onChange={(e) => setForm({...form, conda_env: e.target.value})}>
                    <option value="">사용 안 함 (기본 python)</option>
                    {envOptions.map((env) => (
                      <option key={env.prefix} value={env.name}>
                        {env.name} — {ENV_SOURCE_LABELS[env.source] || "공용"}
                      </option>
                    ))}
                  </select>
                : <input placeholder="예: pytorch1.12.1_p38" value={form.conda_env} onChange={(e) => setForm({...form, conda_env: e.target.value})}/>}
            </Field>
          </article>
          <article className="panel form-panel"><SectionTitle number="02" title="데이터와 결과" subtitle="대용량 데이터는 업로드하지 않고 기존 NAS 경로를 사용합니다."/>
            <Field label="NAS 데이터 경로" hint="압축 파일 하나 · 찾아보기로 고르거나 내 PC에서 올릴 수 있습니다">
              <div className="path-input">
                <input placeholder="예: /data/사용자명/datasets/images.tar.gz" value={form.dataset_path} onChange={(e) => setForm({...form, dataset_path: e.target.value})}/>
                <button onClick={() => setNasOpen(true)}><Icon name="folder" size={17}/> 찾아보기</button>
                <button onClick={uploadDataset} disabled={uploading}>{uploading ? "올리는 중…" : "올리기"}</button>
              </div>
            </Field>
            <label className="switch-row"><button type="button" className="switch on" disabled aria-label="GPU 노드 로컬 복사 필수"><i/></button><div><strong>/local_datasets로 복사·압축 해제</strong><span>튜토리얼 준수를 위해 항상 적용되며 끌 수 없습니다.</span></div></label>
            <Field label="결과 저장 경로"><input value={form.output_path} onChange={(e) => setForm({...form, output_path: e.target.value})}/></Field>
          </article>
          <article className="panel form-panel"><SectionTitle number="03" title="GPU와 실행 조건" subtitle="추천 결과는 원본 SERAPH 코어의 sbatch --test-only를 사용합니다."/>
            <div className="resource-grid"><Field label="GPU"><select value={form.gpus} onChange={(e) => setForm({...form, gpus: e.target.value})}>{[1,2,4,8,16].map(n => <option key={n}>{n}</option>)}</select></Field><Field label="GPU당 CPU"><input type="number" min="1" value={form.cpus} onChange={(e) => setForm({...form, cpus: e.target.value})}/></Field><Field label="GPU당 메모리"><input value={form.memory} onChange={(e) => setForm({...form, memory: e.target.value})}/></Field><Field label="시간 제한"><input value={form.time_limit} onChange={(e) => setForm({...form, time_limit: e.target.value})}/></Field></div>
            <label className="check-row"><input type="checkbox" checked={form.high_perf} onChange={(e) => setForm({...form, high_perf: e.target.checked})}/><span><strong>고성능 GPU 요청</strong><small>별도 권한이 있는 사용자만 선택</small></span></label>
            <div className="field-grid"><Field label="파티션"><select value={form.partition} onChange={(e) => setForm({...form, partition: e.target.value})}><option value="">자동 선택</option>{Object.entries(partitions).map(([name, item]) => <option key={name} value={name} disabled={!item.can_use}>{name}{!item.can_use ? " · 권한 없음" : ""}</option>)}</select></Field><Field label="노드 (선택)"><input placeholder="추천 시 자동 입력" value={form.node} onChange={(e) => setForm({...form, node: e.target.value})}/></Field></div>
            <div className="button-row"><button className="secondary" onClick={recommend} disabled={loading}><Icon name="spark" size={18}/> 위치 추천</button><button className="secondary" onClick={validate} disabled={loading}><Icon name="check" size={18}/> 설정 검사</button><button className="primary" onClick={prepare} disabled={loading}>업로드하고 준비 <Icon name="arrow" size={17}/></button></div>
          </article>
        </div>
        <aside className="review-column">
          <article className="panel sticky-review"><p className="eyebrow">SUBMISSION REVIEW</p><h2>제출 준비 상태</h2>
            {!validation && <div className="empty-review"><div><Icon name="check" size={26}/></div><strong>아직 검사하지 않았습니다</strong><p>코드와 경로, 자원 조건을 입력한 뒤 설정 검사를 실행하세요.</p></div>}
            {validation && <><div className={`validation-head ${validation.ok ? "valid" : "invalid"}`}><Icon name={validation.ok ? "check" : "close"}/><div><strong>{validation.ok ? "검사 통과" : "수정이 필요합니다"}</strong><span>{validation.problems.length}개 안내</span></div></div><div className="problem-list">{validation.problems.length === 0 && <p className="all-clear">차단 또는 경고 항목이 없습니다.</p>}{validation.problems.map((item, index) => <div key={`${item.code}-${index}`} className={item.level}><b>{item.level === "block" ? "차단" : "경고"}</b><div><strong>{item.code}</strong><p>{item.message}</p></div></div>)}</div><dl className="review-data"><div><dt>파티션</dt><dd>{validation.resolved.partition}</dd></div><div><dt>노드</dt><dd>{validation.resolved.node || "자동"}</dd></div><div><dt>코드</dt><dd>{validation.code.display_name}</dd></div><div><dt>업로드</dt><dd>{formatBytes(validation.code.bytes)}</dd></div></dl></>}
            {prepared && <div className="prepared-box"><div className="prepared-title"><span><Icon name="terminal" size={18}/></span><div><strong>스크립트 생성 완료</strong><small>{prepared.job.remote_dir}</small></div><button onClick={() => navigator.clipboard.writeText(prepared.script)} title="복사"><Icon name="copy" size={17}/></button></div><pre>{prepared.script}</pre><button className="secondary full preflight-button" disabled={loading || prepared.preflight?.ok || prepared.job.slurm_job_id} onClick={preflight}><Icon name="terminal" size={16}/>{prepared.preflight?.ok ? "srun 사전 점검 통과" : "srun 사전 점검 실행"}</button>{prepared.preflight && <pre className="preflight-output">{prepared.preflight.output}</pre>}<label className="final-confirm"><input type="checkbox" checked={confirmSubmit} disabled={!prepared.preflight?.ok} onChange={(e) => setConfirmSubmit(e.target.checked)}/><span>{prepared.preflight?.ok ? "srun 점검과 스크립트·경로 확인을 마쳤으며 실제 제출에 동의합니다." : "먼저 srun 사전 점검을 통과해야 최종 제출할 수 있습니다."}</span></label><button className="submit-button" disabled={!prepared.preflight?.ok || !confirmSubmit || loading || prepared.job.slurm_job_id} onClick={submit}>{prepared.job.slurm_job_id ? `제출됨 · ${prepared.job.slurm_job_id}` : "최종 제출"}<Icon name="arrow" size={17}/></button></div>}
          </article>
        </aside></div>
      </section>}

      {tab === "jobs" && <section className="page jobs-page">
        <article className="panel jobs-panel"><div className="panel-head"><div><p className="eyebrow">SLURM JOBS</p><h2>작업별 실행 상태</h2></div><button className="secondary compact" onClick={loadJobs}><Icon name="refresh" size={16}/> 새로고침</button></div><JobTable jobs={jobs} onOpen={openJob}/></article>
        {selected && <DrawerShell onClose={() => setSelected(null)} label="작업 상세"><div className="drawer-head"><div><p className="eyebrow">JOB DETAIL</p><h2>{selected.job.job_name}</h2></div><button onClick={() => setSelected(null)}><Icon name="close"/></button></div><div className="drawer-status"><StatusPill status={selected.job.status}/><span>Slurm #{selected.job.slurm_job_id || "미제출"}</span></div><dl className="detail-grid"><div><dt>파티션</dt><dd>{selected.job.partition}</dd></div><div><dt>노드</dt><dd>{selected.job.node || "자동"}</dd></div><div><dt>GPU</dt><dd>{selected.job.gpus}개</dd></div><div><dt>시간 제한</dt><dd>{selected.job.time_limit}</dd></div><div className="wide"><dt>데이터</dt><dd>{selected.job.dataset_path}</dd></div><div className="wide"><dt>결과</dt><dd>{selected.job.output_path}</dd></div></dl><div className="log-tabs"><span><Icon name="terminal" size={17}/> stdout · 수동 갱신</span><div><button onClick={() => refreshJobLogs(selected.job.local_job_id)}><Icon name="refresh" size={15}/> 로그 갱신</button><button onClick={() => navigator.clipboard.writeText(logs?.stdout || "")}><Icon name="copy" size={15}/> 복사</button></div></div><pre className="logs">{logs?.stdout || "아직 출력 로그가 없습니다."}{logs?.stderr ? `\n\n[stderr]\n${logs.stderr}` : ""}</pre>{ACTIVE.has(selected.job.status) && selected.job.slurm_job_id && <button className="danger-button" onClick={() => cancel(selected.job.local_job_id)}>작업 취소</button>}{!ACTIVE.has(selected.job.status) && <button className="secondary full drawer-delete" onClick={() => removeJob(selected.job.local_job_id)}><Icon name="close" size={15}/> 이 작업 기록 삭제</button>}</DrawerShell>}
      </section>}

      {tab === "envs" && <EnvsPage report={report} onEnvsChanged={setEnvOptions}/>}

      {tab === "history" && <section className="page history-page">
        <div className="welcome-strip history-strip"><div><span className="live-dot"/>SACCT · {historyDays}일</div><p>{jobHistory?.headline || "기간을 선택하면 완료된 작업 이력을 조회합니다."}</p><div className="history-controls">{[1,7,30,60].map((d) => <button key={d} className={d === historyDays ? "on" : ""} onClick={() => loadHistory(d)}>{d}일</button>)}<button className="hs-refresh" onClick={() => loadHistory(historyDays)} title="다시 불러오기"><Icon name="refresh" size={15}/></button></div></div>
        {jobHistory && <div className="metrics history-stats">
          <Metric icon="check" label="성공률" value={`${Math.round(jobHistory.stats.success_rate * 100)}%`} detail={`${jobHistory.stats.total}개 중 ${jobHistory.stats.succeeded}개 성공`} accent="mint"/>
          <Metric icon="close" label="실패한 작업" value={jobHistory.stats.failed} detail={`전체 ${jobHistory.stats.total}개`} accent="violet"/>
          <Metric icon="warn" label="낭비된 GPU 시간" value={`${jobHistory.stats.wasted_gpu_hours}h`} detail="실패 작업이 태운 시간" accent="amber"/>
          <Metric icon="gpu" label="총 GPU 시간" value={`${jobHistory.stats.total_gpu_hours}h`} detail="완료 작업 합계" accent="blue"/>
        </div>}
        {jobHistory && <article className="panel history-panel">
          <div className="panel-head"><div><p className="eyebrow">OUTCOMES</p><h2>상태 분포 · 작업별 결과</h2></div><span>최근 {historyDays}일 · 최대 50개</span></div>
          <StateBreakdown byState={jobHistory.stats.by_state} total={jobHistory.stats.total}/>
          <HistoryTable jobs={jobHistory.jobs} onOpen={setHistoryJob} selectedId={historyJob?.job_id}/>
        </article>}
        {!jobHistory && !loading && <div className="empty-table"><Icon name="history" size={28}/><strong>이력을 불러오세요</strong><span>위에서 기간을 선택하면 완료된 작업을 조회합니다.</span></div>}
        {historyJob && <HistoryDetail job={historyJob} onClose={() => setHistoryJob(null)}/>}
      </section>}

      {tab === "tutorial" && <section className="page tutorial-page">
        <div className="welcome-strip"><div><span className="live-dot"/>PRACTICE</div><p>{tutMode === "practice" ? "명령어를 직접 입력하면 내 실제 클러스터 현황으로 실행됩니다. 오른쪽 단계를 따라가 보세요." : "세라프 사용 흐름과 주의사항을 읽기 자료로 정리했습니다."}</p><div className="tut-mode-toggle"><button className={tutMode === "practice" ? "on" : ""} onClick={() => setTutMode("practice")}><Icon name="terminal" size={14}/> 실습 터미널</button><button className={tutMode === "read" ? "on" : ""} onClick={() => setTutMode("read")}><Icon name="book" size={14}/> 안내 보기</button></div></div>
        {!tutorial && <div className="empty-table"><Icon name="book" size={28}/><strong>튜토리얼을 불러오는 중…</strong></div>}
        {tutorial && tutMode === "practice" && <TutorialTerminal steps={tutorial.steps} user={tutorial.user || me?.user}/>}
        {tutorial && tutMode === "read" && <div className="tutorial-steps">{tutorial.steps.map((step, i) => <TutorialStep key={step.id} step={step} n={i + 1}/>)}</div>}
      </section>}

      {tab === "notices" && <section className="page notices-page">
        <div className="welcome-strip"><div><span className="live-dot"/>SLACK · {announcements?.channel || "공지"}</div><p>{announcements?.ok === false ? "공지를 불러오지 못했습니다." : "관리자 공지입니다. 노드 점검·정책 변경 등을 확인하세요."}</p><button onClick={loadAnnouncements}>새로고침 <Icon name="refresh" size={15}/></button></div>
        {!announcements && <div className="empty-table"><Icon name="bell" size={28}/><strong>공지를 불러오는 중…</strong></div>}
        {announcements?.ok === false && <div className="notice-error"><Icon name="warn" size={18}/><p>{announcements.message || "Slack 공지를 읽지 못했습니다."}</p></div>}
        {announcements?.announcements?.length > 0 && <div className="notice-list">{announcements.announcements.map((a) => <AnnouncementCard key={a.ts} a={a}/>)}</div>}
        {announcements?.ok && !announcements.announcements?.length && <div className="empty-table"><Icon name="bell" size={28}/><strong>공지가 없습니다</strong><span>새 공지가 올라오면 여기에 표시됩니다.</span></div>}
      </section>}
    </main>

    <NasBrowser open={nasOpen} onClose={() => setNasOpen(false)} onPick={pickDataset} onUpload={uploadDataset} uploading={uploading}/>
    {health && !health.seraph_reachable && <ConnectCard mode={health.mode} clusters={clusterInfo?.clusters} routing={clusterInfo?.routing} health={health} loading={loading} onConnect={connect}/>}
    {loading && <div className="loading-line"/>}
    <AlertBanner alerts={alerts} onDismiss={dismissAlert}/>
    <ErrorToast error={error} onClose={() => setError(null)}/>
  </div>;
}

function SectionTitle({ number, title, subtitle }) { return <div className="section-title"><span>{number}</span><div><h2>{title}</h2><p>{subtitle}</p></div></div>; }
function Field({ label, hint, children }) { return <label className="field"><span>{label}{hint && <small>{hint}</small>}</span>{children}</label>; }
function formatBytes(bytes) { if (bytes == null) return "—"; if (bytes < 1024) return `${bytes} B`; if (bytes < 1048576) return `${(bytes/1024).toFixed(1)} KB`; return `${(bytes/1048576).toFixed(1)} MB`; }

function JobTable({ jobs, onOpen }) {
  if (!jobs.length) return <div className="empty-table"><Icon name="jobs" size={28}/><strong>아직 준비한 작업이 없습니다</strong><span>새 작업 화면에서 첫 GPU 작업을 만들어 보세요.</span></div>;
  return <div className="jobs-table"><div className="job-row job-header"><span>작업</span><span>상태</span><span>자원</span><span>파티션 · 노드</span><span>Slurm ID</span><span/></div>{jobs.map((job) => <button className="job-row" key={job.local_job_id} onClick={() => onOpen(job.local_job_id)}><span className="job-name"><i>{job.job_name?.slice(0, 1).toUpperCase()}</i><span><strong>{job.job_name}</strong><small>{job.created_at ? new Date(job.created_at).toLocaleString("ko-KR", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"}) : ""}</small></span></span><StatusPill status={job.status}/><span>{job.gpus} GPU · CPU {job.cpus}/GPU</span><span>{job.partition}<small>{job.node || "자동 선택"}</small></span><code>{job.slurm_job_id || "—"}</code><Icon name="arrow" size={17}/></button>)}</div>;
}

function formatEta(iso) {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return { abs: "—", rel: "" };
  const now = new Date();
  const time = t.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
  // 며칠 뒤 시작인데 시:분만 보여주면 오늘로 오해한다(세라프는 대기가 며칠 가는 게 흔하다).
  // 날짜가 다르면 날짜를 함께 찍는다.
  const sameDay = t.toDateString() === now.toDateString();
  const abs = sameDay ? time : `${t.getMonth() + 1}/${t.getDate()} ${time}`;
  const diff = (t.getTime() - now.getTime()) / 1000;
  if (diff <= 60) return { abs, rel: "곧" };
  const d = Math.floor(diff / 86400);
  const h = Math.floor((diff % 86400) / 3600);
  const m = Math.floor((diff % 3600) / 60);
  // 며칠짜리 대기를 "약 91시간 뒤"로 쓰면 감이 안 온다.
  const rel = d > 0 ? `약 ${d}일 ${h}시간 뒤` : h > 0 ? `약 ${h}시간 ${m}분 뒤` : `약 ${m}분 뒤`;
  return { abs, rel };
}

function EtaBadge({ start, confidence }) {
  if (!start) return <span className="eta-badge unknown">시작 시각 미정</span>;
  const { abs, rel } = formatEta(start);
  const cls = confidence === "medium" ? "ok" : confidence === "low" ? "low" : "unknown";
  const uncertain = confidence !== "medium";
  return <span className={`eta-badge ${cls}`} title={uncertain ? "Slurm 추정이 부정확할 수 있습니다" : "Slurm 추정 기준"}>{abs}{rel ? ` · ${rel}` : ""}{uncertain ? " (추정)" : ""}</span>;
}

function EtaItem({ job }) {
  return <li className={`eta-item ${job.blocked_by_quota ? "quota" : ""}`}>
    <div className="eta-top"><strong>{job.name || job.job_id}</strong><EtaBadge start={job.estimated_start} confidence={job.confidence}/></div>
    <p className="eta-reason">{job.reason_text || job.reason}{job.requested_gpus ? ` · GPU ${job.requested_gpus}개` : ""}</p>
    {job.advice && <p className="eta-advice">{job.advice}</p>}
  </li>;
}

function QueueTable({ pending }) {
  if (!pending.length) return <div className="empty-mini"><Icon name="jobs" size={20}/><span>대기 중인 작업이 없습니다.</span></div>;
  const shown = pending.slice(0, 12);
  return <div className="queue-table">
    <div className="queue-row queue-header"><span>#</span><span>작업 · 사용자</span><span>GPU</span><span>대기 사유</span><span>예상 시작</span></div>
    {shown.map((j) => <div className={`queue-row ${j.is_mine ? "mine" : ""}`} key={j.job_id}>
      <span className="qpos">{j.queue_position}</span>
      <span className="qname"><strong>{j.name || j.job_id}</strong><small>{j.is_mine ? "나" : j.user}</small></span>
      <span className="qgpu">{j.gpus}{j.high_perf_gpus ? " · 고성능" : ""}</span>
      <span className={`qreason ${j.blocked_by_quota ? "quota" : ""}`}>{j.reason_text || j.reason}</span>
      <span className="qeta">{j.estimated_start ? formatEta(j.estimated_start).abs : "—"}{j.estimated_start && j.confidence !== "medium" && <em>추정</em>}</span>
    </div>)}
    {pending.length > shown.length && <div className="queue-more">외 {pending.length - shown.length}개 더 대기 중</div>}
  </div>;
}

function TrendChart({ samples }) {
  if (!samples || samples.length < 2) {
    return <div className="trend-empty"><Icon name="spark" size={22}/><span>점유 추세를 수집하는 중입니다. 대시보드가 갱신되면 채워집니다.</span></div>;
  }
  const W = 680, H = 132, pad = 8, n = samples.length;
  const util = samples.map((s) => Math.min(1, Math.max(0, s.utilization)));
  const x = (i) => pad + (i / (n - 1)) * (W - 2 * pad);
  const y = (v) => pad + (1 - v) * (H - 2 * pad);
  const line = util.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${x(n - 1).toFixed(1)},${(H - pad).toFixed(1)} L${x(0).toFixed(1)},${(H - pad).toFixed(1)} Z`;
  const last = samples[n - 1];
  return <div className="trend-wrap">
    <div className="trend-legend">
      <div><strong>{Math.round(last.utilization * 100)}%</strong><span>현재 점유율</span></div>
      <div><strong>{last.free_gpus}</strong><span>사용 가능 GPU</span></div>
      <div><strong>{last.pending_jobs}</strong><span>대기 작업</span></div>
      <div><strong>{last.running_jobs}</strong><span>실행 작업</span></div>
    </div>
    <svg className="trend-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="GPU 점유율 추세">
      <defs><linearGradient id="trendfill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#21bd91" stopOpacity="0.28"/><stop offset="100%" stopColor="#21bd91" stopOpacity="0"/></linearGradient></defs>
      <line x1={pad} y1={y(0.5)} x2={W - pad} y2={y(0.5)} stroke="#e4e8f0" strokeWidth="1" strokeDasharray="3 4" vectorEffect="non-scaling-stroke"/>
      <path d={area} fill="url(#trendfill)"/>
      <path d={line} fill="none" stroke="#17ac82" strokeWidth="2.2" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke"/>
      <circle cx={x(n - 1)} cy={y(util[n - 1])} r="3.4" fill="#17ac82" vectorEffect="non-scaling-stroke"/>
    </svg>
  </div>;
}

const STATE_COLOR = { COMPLETED: "#23bd91", FAILED: "#df5965", TIMEOUT: "#e9a23b", OUT_OF_MEMORY: "#e07a4b", CANCELLED: "#8b96a7" };
const STATE_LABEL = { COMPLETED: "완료", FAILED: "실패", TIMEOUT: "시간초과", OUT_OF_MEMORY: "메모리부족", CANCELLED: "취소" };
function stateLabel(st) { return STATE_LABEL[(st || "").toUpperCase()] || st; }
function formatDuration(s) { if (s == null) return "무제한"; const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); if (h) return `${h}시간 ${m}분`; if (m) return `${m}분`; return `${s}초`; }
function formatMB(mb) { if (mb == null) return "—"; return mb < 1024 ? `${mb}MB` : `${(mb / 1024).toFixed(1)}GB`; }
function fmtTime(iso) { if (!iso) return "—"; const t = new Date(iso); return Number.isNaN(t.getTime()) ? iso : t.toLocaleString("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }); }

function StateBreakdown({ byState, total }) {
  const entries = Object.entries(byState || {}).sort((a, b) => b[1] - a[1]);
  if (!total) return null;
  return <div className="state-breakdown">
    <div className="sb-bar">{entries.map(([st, n]) => <i key={st} style={{ width: `${n / total * 100}%`, background: STATE_COLOR[st] || "#b6c1d1" }} title={`${stateLabel(st)} ${n}`}/>)}</div>
    <div className="sb-legend">{entries.map(([st, n]) => <span key={st}><b style={{ background: STATE_COLOR[st] || "#b6c1d1" }}/>{stateLabel(st)} {n}</span>)}</div>
  </div>;
}

function HistoryTable({ jobs, onOpen, selectedId }) {
  if (!jobs.length) return <div className="empty-mini"><Icon name="history" size={20}/><span>해당 기간에 완료된 작업이 없습니다.</span></div>;
  return <div className="history-table">
    <div className="hrow hhead"><span>작업</span><span>상태</span><span>파티션 · 노드</span><span>GPU</span><span>실행 시간</span><span>원인</span></div>
    {jobs.map((j) => <button key={j.job_id} className={`hrow ${selectedId === j.job_id ? "sel" : ""} ${j.succeeded ? "" : "isfail"}`} onClick={() => onOpen(j)}>
      <span className="hname"><strong>{j.name}</strong><small>#{j.job_id}</small></span>
      <StatusPill status={j.state}/>
      <span className="hpart">{j.partition}<small>{j.nodes || "—"}</small></span>
      <span>{j.gpus}{j.high_perf_gpus ? " · 고성능" : ""}</span>
      <span className="hela">{formatDuration(j.elapsed_seconds)}</span>
      <span className={`hreason ${j.succeeded ? "ok" : "bad"}`}>{j.succeeded ? "정상 종료" : j.reason_text}</span>
    </button>)}
  </div>;
}

function fmtNotice(iso) { if (!iso) return ""; const t = new Date(iso); return Number.isNaN(t.getTime()) ? iso : t.toLocaleString("ko-KR", { month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" }); }

function AnnouncementCard({ a }) {
  const urgent = (a.text || "").includes("[긴급]");
  return <article className={`panel notice-card ${urgent ? "urgent" : ""}`}>
    <div className="notice-head"><span className="na-avatar">{(a.author || "?").slice(0, 1).toUpperCase()}</span><div className="na-meta"><strong>{a.author}{a.is_bot && <em className="bot-tag">BOT</em>}</strong><small>{fmtNotice(a.posted_at)}</small></div></div>
    <p className="notice-text">{a.text}</p>
    {(a.reactions?.length > 0 || a.reply_count > 0) && <div className="notice-foot">{a.reactions?.map((r) => <span className="reaction" key={r.name}>{r.name} · {r.count}</span>)}{a.reply_count > 0 && <span className="replies">답글 {a.reply_count}</span>}</div>}
  </article>;
}

function fmtDurShort(s) { if (s == null) return "-"; const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h ? `${h}h${m}m` : `${m}m`; }

const TERM_TASKS = [
  { id: "whoami", stepId: "ssh", label: "접속 확인", answer: "whoami", match: /^\s*whoami\s*$/, hint: "whoami — 지금 접속한 계정을 확인합니다." },
  { id: "quota", stepId: "quota", label: "내 할당량", answer: "show-qos", match: /^\s*(show-qos|show-assoc)\b/, hint: "show-qos — 내 GPU/고성능/동시잡 한도를 봅니다. 세라프 대기의 대부분이 이 한도 때문입니다." },
  { id: "queue", stepId: "status", label: "대기열 보기", answer: "squeue", match: /^\s*squeue\b/, hint: "squeue — 지금 대기 줄과 예상 시작 순번을 봅니다." },
  { id: "status", stepId: "status", label: "GPU 현황", answer: "slurm-gres-viz -i", match: /^\s*(slurm-gres-viz|sinfo)\b/, hint: "slurm-gres-viz -i — 노드별로 '실제로 쓸 수 있는' GPU를 봅니다 (CPU 없으면 못 씀)." },
  { id: "result", stepId: "result", label: "완료 이력", answer: "sacct", match: /^\s*sacct\b/, hint: "sacct — 끝난 작업의 상태·실패 원인을 봅니다. OOM은 종료코드가 0이라 State를 꼭 봐야 합니다." },
  { id: "submit", stepId: "submit", label: "제출 연습", answer: "srun --gres=gpu:1 -p debug_grad --pty $SHELL", match: /^\s*(srun|sbatch)\b/, hint: "srun/sbatch — 여기선 시뮬레이션. 실제 학습 제출은 '새 작업' 탭에서 안전하게 합니다." },
];

async function execTutorialCmd(raw, uname) {
  const name = raw.trim().split(/\s+/)[0];
  const L = (t, c) => ({ t, c: c || "" });
  if (name === "clear") return "CLEAR";
  if (name === "help") return [
    L("사용 가능한 명령 — 내 실제 클러스터 데이터로 실행됩니다:", "c-acc"),
    L("  whoami                      내 계정"),
    L("  show-qos                    내 QOS 한도(GPU/고성능/동시잡)"),
    L("  squeue [-u $USER]           대기열(순번·예상 시작)"),
    L("  slurm-gres-viz -i / sinfo   노드별 사용 가능 GPU"),
    L("  sacct                       끝난 작업 이력·실패 원인"),
    L("  srun / sbatch               제출 연습(시뮬레이션)"),
    L("  clear                       화면 지우기"),
  ];
  if (name === "whoami") { return [L(uname)]; }
  if (name === "show-qos" || name === "show-assoc") {
    const u = await api("/api/v1/cluster/usage");
    const lim = (v) => v == null ? "∞" : v;
    return [
      L("QOS 한도 / 사용량 (내 계정)", "c-acc"),
      L(`  GPU            ${u.gpus_in_use} / ${lim(u.gpus_limit)}`),
      L(`  고성능 GPU      ${u.high_perf_in_use} / ${u.high_perf_limit === 0 ? "0 (사용 불가)" : lim(u.high_perf_limit)}`),
      L(`  동시 실행 job   ${u.running_jobs} / ${lim(u.running_jobs_limit)}`),
      L(`  제출 job        ${u.submitted_jobs} / ${lim(u.submit_jobs_limit)}`),
      L(""),
      L("세라프 대기의 대부분은 GPU 부족이 아니라 이 한도 초과입니다.", "c-dim"),
    ];
  }
  if (name === "squeue") {
    const mine = /\-u\b|\$USER/.test(raw);
    const q = await api("/api/v1/queue");
    let rows = q.pending || [];
    if (mine) rows = rows.filter((r) => r.is_mine);
    const out = [
      L(`대기열: 대기 ${q.pending_count} · 실행 ${q.running_count}` + (q.my_next_position ? ` · 내 순번 ${q.my_next_position}` : ""), "c-acc"),
      L(" 순번  JOBID     사용자    사유                예상시작", "c-dim"),
    ];
    if (!rows.length) out.push(L(`  (대기 중인 ${mine ? "내 " : ""}작업이 없습니다)`, "c-dim"));
    rows.slice(0, 10).forEach((r) => out.push(L(
      `  ${String(r.queue_position).padStart(3)}  ${String(r.job_id).padEnd(8)}  ${String(r.is_mine ? "나" : r.user).padEnd(7).slice(0, 7)}  ${String(r.reason_text || r.reason).padEnd(18).slice(0, 18)}  ${r.estimated_start ? r.estimated_start.slice(11, 16) : "-"}`,
      r.is_mine ? "c-acc" : "")));
    if (rows.length > 10) out.push(L(`  ... 외 ${rows.length - 10}개`, "c-dim"));
    return out;
  }
  if (name === "slurm-gres-viz" || name === "sinfo") {
    const s = await api("/api/v1/cluster/status");
    const out = [
      L(`GPU 현황 · ${s.partition}`, "c-acc"),
      L(`  사용 가능 ${s.free_gpus} / 전체 ${s.total_gpus}  (고성능 ${s.free_high_perf_gpus} · 일반 ${s.free_standard_gpus})`),
    ];
    if (s.idle_but_unusable_gpus) out.push(L(`  ⚠ ${s.idle_but_unusable_gpus}개는 비었지만 CPU가 없어 사용 불가`, "c-warn"));
    out.push(L(""), L(" 노드         가용/전체", "c-dim"));
    (s.nodes || []).slice(0, 10).forEach((n) => {
      const total = n.total_gpus || 1;
      const fill = n.usable_gpus ? Math.max(1, Math.round((n.usable_gpus / total) * 10)) : 0;
      const bar = "▓".repeat(fill) + "░".repeat(10 - fill);
      out.push(L(`  ${n.name.padEnd(11)} ${String(n.usable_gpus).padStart(2)}/${total}  ${bar}`, n.usable_gpus ? "c-ok" : "c-dim"));
    });
    return out;
  }
  if (name === "sacct") {
    const h = await api("/api/v1/jobs/history?days=7&limit=8");
    const out = [L(h.headline, "c-acc"), L(" 작업              상태         실행    원인", "c-dim")];
    (h.jobs || []).forEach((j) => out.push(L(
      `  ${String(j.name || "").padEnd(16).slice(0, 16)}  ${String(j.state || "").padEnd(11).slice(0, 11)}  ${fmtDurShort(j.elapsed_seconds).padStart(5)}   ${j.succeeded ? "정상" : (j.reason_text || "")}`,
      j.succeeded ? "" : "c-err")));
    return out;
  }
  if (name === "srun") return [
    L("srun: (연습) 인터랙티브 세션을 요청하는 명령입니다.", "c-dim"),
    L("실제로 이 도구는 debug 파티션에서 5분 srun 으로 코드·GPU·conda를 점검합니다.", "c-dim"),
    L("👉 실제 학습 제출은 왼쪽 '새 작업' 탭에서 안전하게 진행하세요.", "c-acc"),
  ];
  if (name === "sbatch") return [
    L("sbatch: (연습) 배치 학습을 제출하는 명령입니다.", "c-dim"),
    L("이 도구는 절대 시작 못하는 job을 미리 막고, 검증된 스크립트를 만들어 제출합니다.", "c-dim"),
    L("👉 '새 작업' 탭 → 설정 검사 → srun 점검 → 최종 제출.", "c-acc"),
  ];
  if (name === "ls") return [L("data/  logs/  train.py  train.sh")];
  if (name === "pwd") return [L(`/data/${uname}`)];
  if (name === "cd" || name === "") return [];
  return [L(`${name}: command not found — 'help' 로 사용 가능한 명령을 확인하세요.`, "c-err")];
}

function TutorialTerminal({ steps, user }) {
  const uname = user || "user01";
  const [lines, setLines] = useState(() => [
    { t: "SERAPH 실습 터미널 — 내 실제 클러스터 현황으로 명령을 실행합니다.", c: "c-acc" },
    { t: "오른쪽 단계를 따라 명령을 직접 입력하세요. 'help' 로 명령 목록을 봅니다.", c: "c-dim" },
    { t: "", c: "" },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [stepIdx, setStepIdx] = useState(0);
  const [hist, setHist] = useState([]);
  const [histAt, setHistAt] = useState(-1);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => { const el = scrollRef.current; if (el) el.scrollTop = el.scrollHeight; }, [lines, busy]);

  const run = async (raw) => {
    setLines((old) => [...old, { t: `${uname}@ariel-master:~$ ${raw}`, c: "cmd" }]);
    const cmd = raw.trim();
    if (cmd) setHist((h) => [...h, cmd]);
    setHistAt(-1); setInput("");
    if (!cmd) return;
    setBusy(true);
    let ok = false;
    try {
      const out = await execTutorialCmd(cmd, uname);
      if (out === "CLEAR") setLines([]);
      else setLines((old) => [...old, ...out]);
      ok = true;
    } catch {
      setLines((old) => [...old, { t: "명령 실행 중 오류 (백엔드 연결을 확인하세요).", c: "c-err" }]);
    }
    setBusy(false);
    // 명령이 실제로 성공했을 때만 단계를 진행한다(백엔드 오류 시 완료로 오인 방지).
    if (ok) {
      const task = TERM_TASKS[stepIdx];
      if (task && task.match.test(cmd)) setStepIdx((i) => Math.min(i + 1, TERM_TASKS.length));
    }
  };

  const onKey = (e) => {
    if (e.key === "Enter") { e.preventDefault(); if (!busy) run(input); }
    else if (e.key === "ArrowUp") { e.preventDefault(); if (hist.length) { const at = histAt < 0 ? hist.length - 1 : Math.max(0, histAt - 1); setHistAt(at); setInput(hist[at]); } }
    else if (e.key === "ArrowDown") { e.preventDefault(); if (histAt >= 0) { const at = histAt + 1; if (at >= hist.length) { setHistAt(-1); setInput(""); } else { setHistAt(at); setInput(hist[at]); } } }
  };

  const done = stepIdx >= TERM_TASKS.length;
  const task = TERM_TASKS[stepIdx];
  const relStep = task && steps?.find((s) => s.id === task.stepId);

  return <div className="tut-terminal-wrap">
    <div className="tut-term" onClick={() => inputRef.current?.focus()}>
      <div className="tut-term-bar"><span className="tt-dot"/><span className="tt-dot"/><span className="tt-dot"/><span className="tt-title">{uname}@ariel-master — practice</span></div>
      <div className="tut-term-screen" ref={scrollRef}>
        {lines.map((ln, i) => <div key={i} className={`tt-line ${ln.c || ""}`}>{ln.t || " "}</div>)}
        {busy && <div className="tt-line c-dim">…</div>}
        <div className="tt-inputline"><span className="tt-ps1">{uname}@ariel-master:~$</span><input ref={inputRef} className="tt-input" value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={onKey} spellCheck="false" autoCapitalize="off" autoComplete="off" placeholder={stepIdx === 0 ? "여기에 입력…" : ""}/></div>
      </div>
    </div>
    <aside className="tut-guide">
      <div className="tut-guide-prog"><i style={{ width: `${stepIdx / TERM_TASKS.length * 100}%` }}/></div>
      {!done ? <>
        <div className="tut-guide-eyebrow">STEP {stepIdx + 1} / {TERM_TASKS.length}</div>
        <h3 className="tut-guide-title">{task.label}</h3>
        <p className="tut-guide-hint">{task.hint}</p>
        <div className="tut-guide-cmd"><code onClick={() => { if (!busy) run(task.answer); }} title="터미널에서 바로 실행 (자동 입력 + 엔터)">{task.answer}</code><button onClick={() => navigator.clipboard.writeText(task.answer)} title="클립보드에 복사"><Icon name="copy" size={13}/></button></div>
        {relStep?.pitfall && <div className="tut-guide-pitfall"><Icon name="warn" size={14}/><p>{relStep.pitfall}</p></div>}
      </> : <div className="tut-guide-done"><div className="tgd-badge"><Icon name="check" size={22}/></div><strong>실습 완료!</strong><p>기본 명령을 다 익혔습니다. 이제 '새 작업' 탭에서 실제로 학습을 제출해 보세요.</p></div>}
      <ol className="tut-guide-steps">{TERM_TASKS.map((t, i) => <li key={t.id} className={i < stepIdx ? "done" : i === stepIdx ? "active" : ""}><span className="tgs-ck">{i < stepIdx ? "✓" : i + 1}</span>{t.label}</li>)}</ol>
    </aside>
  </div>;
}

function TutorialStep({ step, n }) {
  return <article className="panel tut-step">
    <div className="tut-head"><span className="tut-num">{String(n).padStart(2, "0")}</span><h2>{step.title}</h2></div>
    <p className="tut-body">{step.body}</p>
    {step.commands?.length > 0 && <div className="tut-cmds">{step.commands.map((c, i) => <div className="tut-cmd" key={i}><code>{c}</code><button onClick={() => navigator.clipboard.writeText(c.split("#")[0].trim())} title="명령만 복사"><Icon name="copy" size={14}/></button></div>)}</div>}
    {step.pitfall && <div className="tut-pitfall"><Icon name="warn" size={16}/><p>{step.pitfall}</p></div>}
  </article>;
}

function HistoryDetail({ job, onClose }) {
  const memPct = job.req_mem_mb && job.max_rss_mb != null ? Math.min(100, Math.round(job.max_rss_mb / job.req_mem_mb * 100)) : null;
  return <DrawerShell onClose={onClose} label="완료 작업 상세">
    <div className="drawer-head"><div><p className="eyebrow">JOB RESULT</p><h2>{job.name}</h2></div><button onClick={onClose}><Icon name="close"/></button></div>
    <div className="drawer-status"><StatusPill status={job.state}/><span>Slurm #{job.job_id}</span></div>
    <div className={`history-advice ${job.succeeded ? "ok" : "bad"}`}><Icon name={job.succeeded ? "check" : "warn"} size={18}/><p>{job.succeeded ? "정상적으로 완료되었습니다." : (job.advice || job.reason_text)}</p></div>
    {memPct != null && <div className="mem-block"><div className="mem-head"><span>메모리 사용 (MaxRSS / 요청)</span><strong className={memPct >= 90 ? "danger" : ""}>{memPct}%</strong></div><div className="mem-bar"><i className={memPct >= 90 ? "danger" : ""} style={{ width: `${memPct}%` }}/></div><div className="mem-sub">{formatMB(job.max_rss_mb)} / {formatMB(job.req_mem_mb)}</div></div>}
    <dl className="detail-grid">
      <div><dt>상태(원문)</dt><dd>{job.raw_state}</dd></div>
      <div><dt>원인</dt><dd>{job.reason_text}</dd></div>
      <div><dt>GPU</dt><dd>{job.gpus}개{job.high_perf_gpus ? ` (고성능 ${job.high_perf_gpus})` : ""}</dd></div>
      <div><dt>파티션 · 노드</dt><dd>{job.partition} · {job.nodes || "—"}</dd></div>
      <div><dt>실행 시간</dt><dd>{formatDuration(job.elapsed_seconds)} / 제한 {formatDuration(job.time_limit_seconds)}</dd></div>
      <div><dt>종료 코드</dt><dd>{job.exit_code}{job.signal ? ` · signal ${job.signal}` : ""}</dd></div>
      <div className="wide"><dt>시작 → 종료</dt><dd>{fmtTime(job.start)} → {fmtTime(job.end)}</dd></div>
      {job.cancelled_by && <div className="wide"><dt>취소 주체</dt><dd>{job.cancelled_by}</dd></div>}
    </dl>
  </DrawerShell>;
}
