import { useCallback, useEffect, useMemo, useState } from "react";

const ACTIVE = new Set(["SUBMITTED", "SUBMITTING", "PENDING", "RUNNING", "COMPLETING", "CANCEL_REQUESTED"]);
const DASHBOARD_POLL_MS = 60_000;
const ACTIVE_JOB_POLL_MS = 20_000;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
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

function ErrorToast({ error, onClose }) {
  if (!error) return null;
  return <div className="toast" role="alert">
    <div><strong>{error.code || "오류"}</strong><p>{error.message}</p></div>
    <button onClick={onClose} aria-label="닫기"><Icon name="close" size={18}/></button>
  </div>;
}

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
  const [password, setPassword] = useState("");
  const [sshUsername, setSshUsername] = useState("");
  const [sshHost, setSshHost] = useState("");
  const [sshPort, setSshPort] = useState("");
  const [pageVisible, setPageVisible] = useState(() => document.visibilityState === "visible");

  const report = useCallback((err) => setError({ code: err.code, message: err.message }), []);

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
      api("/api/v1/clusters").then(setClusterInfo).catch(() => {});  // 정적 안내, 1회만
      setSshUsername((old) => old || data.ssh_username || "");
      setSshHost((old) => old || data.ssh_host || "ariel.khu.ac.kr");
      setSshPort((old) => old || String(data.ssh_port || 30080));
      if (data.seraph_reachable) await Promise.all([loadDashboard(), loadJobs()]);
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

  const connect = () => runAction(async () => {
    await api("/api/v1/session/connect", {
      method: "POST",
      body: JSON.stringify({
        username: sshUsername || null,
        host: sshHost || null,
        port: sshPort ? Number(sshPort) : null,
        password: password || null,
      }),
    });
    setPassword(""); await initialize();
  });

  const refreshAll = () => runAction(async () => {
    await api("/api/v1/cluster/refresh", { method: "POST" }); await Promise.all([loadDashboard(), loadJobs()]);
  });

  const nav = [
    ["dashboard", "grid", "대시보드"], ["new", "plus", "새 작업"], ["jobs", "jobs", "내 작업"], ["history", "history", "완료 이력"], ["tutorial", "book", "튜토리얼"], ["notices", "bell", "공지"],
  ];
  const visibleNodes = cluster?.nodes?.slice(0, 8) || nodes.slice(0, 8);

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><span /><span /><span /></div><div><strong>SERAPH</strong><small>GPU CONSOLE</small></div></div>
      <nav>{nav.map(([key, icon, label]) => <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}><Icon name={icon}/><span>{label}</span>{key === "jobs" && jobs.length > 0 && <b>{jobs.length}</b>}</button>)}</nav>
      <div className="side-note"><Icon name="server"/><div><strong>{health?.mode === "ssh" ? "SERAPH 연결" : "Mock 시연 모드"}</strong><span>{health?.seraph_reachable ? "정상 연결됨" : "연결 필요"}</span></div><i className={health?.seraph_reachable ? "online" : ""}/></div>
      <p className="version">Local console · v1.1.1</p>
    </aside>

    <main>
      <header>
        <div><p className="eyebrow">{tab === "dashboard" ? "CLUSTER OVERVIEW" : tab === "new" ? "JOB WIZARD" : tab === "history" ? "JOB HISTORY" : tab === "tutorial" ? "GUIDE" : tab === "notices" ? "ANNOUNCEMENTS" : "JOB MONITOR"}</p><h1>{tab === "dashboard" ? "클러스터 대시보드" : tab === "new" ? "새 GPU 작업" : tab === "history" ? "완료 작업 이력" : tab === "tutorial" ? "세라프 사용법" : tab === "notices" ? "공지사항" : "내 작업"}</h1></div>
        <div className="header-actions"><div className="user-chip"><span>{(me?.user || "U").slice(0, 1).toUpperCase()}</span><div><strong>{me?.user || "연결 대기"}</strong><small>{me?.account || health?.mode || "local"}</small></div></div><button className="icon-button" onClick={refreshAll} disabled={loading}><Icon name="refresh"/></button></div>
      </header>

      {tab === "dashboard" && <section className="page dashboard-page">
        <div className="welcome-strip"><div><span className="live-dot"/>LIVE · {cluster?.partition || me?.default_partition || "SERAPH"}</div><p>{diagnosis?.headline || "GPU 현황과 내 작업 상태를 불러오는 중입니다."}</p><button onClick={() => setTab("new")}>새 작업 만들기 <Icon name="arrow" size={16}/></button></div>
        <div className="metrics">
          <Metric icon="gpu" label="사용 가능한 GPU" value={cluster?.free_gpus} detail={`전체 ${cluster?.total_gpus ?? "—"}개`} accent="mint"/>
          <Metric icon="server" label="GPU 사용률" value={cluster ? `${Math.round(cluster.utilization * 100)}%` : null} detail={`${cluster?.used_gpus ?? "—"}개 사용 중`} accent="blue"/>
          <Metric icon="jobs" label="실행 중인 작업" value={cluster?.running_jobs} detail={`대기 ${cluster?.pending_jobs ?? "—"}개`} accent="violet"/>
          <Metric icon="spark" label="내 GPU 사용" value={usage ? `${usage.gpus_in_use}/${usage.gpus_limit ?? "∞"}` : null} detail={`${usage?.pending_jobs ?? "—"}개 대기`} accent="amber"/>
        </div>
        <article className="panel trend-panel">
          <div className="panel-head"><div><p className="eyebrow">OCCUPANCY TREND</p><h2>GPU 점유 추세</h2></div><span>{history.length}개 표본 · 폴링마다 기록</span></div>
          <TrendChart samples={history}/>
        </article>
        <div className="dashboard-grid">
          <article className="panel resource-panel"><div className="panel-head"><div><p className="eyebrow">RESOURCE MAP</p><h2>노드 가용 현황</h2></div><span>사용 가능 GPU 기준</span></div>
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
          <div className="cluster-cards">{Object.entries(clusterInfo.clusters).map(([name, c]) => <div className={`cluster-card ${me?.cluster === name ? "mine" : ""} ${c.connectable ? "" : "muted"}`} key={name}>
            <div className="cc-head"><strong>{name}</strong><span className="cc-tags">{c.connectable && <em className="cc-live">실시간</em>}{me?.cluster === name && <em className="cc-mine">내 소속</em>}</span></div>
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
            <Field label="Conda 환경 (선택)"><input placeholder="예: pytorch-2.6" value={form.conda_env} onChange={(e) => setForm({...form, conda_env: e.target.value})}/></Field>
          </article>
          <article className="panel form-panel"><SectionTitle number="02" title="데이터와 결과" subtitle="대용량 데이터는 업로드하지 않고 기존 NAS 경로를 사용합니다."/>
            <Field label="NAS 데이터 경로"><input value={form.dataset_path} onChange={(e) => setForm({...form, dataset_path: e.target.value})}/></Field>
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
        {selected && <div className="job-drawer"><div className="drawer-head"><div><p className="eyebrow">JOB DETAIL</p><h2>{selected.job.job_name}</h2></div><button onClick={() => setSelected(null)}><Icon name="close"/></button></div><div className="drawer-status"><StatusPill status={selected.job.status}/><span>Slurm #{selected.job.slurm_job_id || "미제출"}</span></div><dl className="detail-grid"><div><dt>파티션</dt><dd>{selected.job.partition}</dd></div><div><dt>노드</dt><dd>{selected.job.node || "자동"}</dd></div><div><dt>GPU</dt><dd>{selected.job.gpus}개</dd></div><div><dt>시간 제한</dt><dd>{selected.job.time_limit}</dd></div><div className="wide"><dt>데이터</dt><dd>{selected.job.dataset_path}</dd></div><div className="wide"><dt>결과</dt><dd>{selected.job.output_path}</dd></div></dl><div className="log-tabs"><span><Icon name="terminal" size={17}/> stdout · 수동 갱신</span><div><button onClick={() => refreshJobLogs(selected.job.local_job_id)}><Icon name="refresh" size={15}/> 로그 갱신</button><button onClick={() => navigator.clipboard.writeText(logs?.stdout || "")}><Icon name="copy" size={15}/> 복사</button></div></div><pre className="logs">{logs?.stdout || "아직 출력 로그가 없습니다."}{logs?.stderr ? `\n\n[stderr]\n${logs.stderr}` : ""}</pre>{ACTIVE.has(selected.job.status) && selected.job.slurm_job_id && <button className="danger-button" onClick={() => cancel(selected.job.local_job_id)}>작업 취소</button>}</div>}
      </section>}

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
        <div className="welcome-strip"><div><span className="live-dot"/>PRACTICE</div><p>세라프 GPU 클러스터 사용 흐름입니다. 도구가 막아주지 못하는 주의사항까지 단계별로 안내합니다. (연습용 · 실서버에 영향 없음)</p></div>
        {!tutorial && <div className="empty-table"><Icon name="book" size={28}/><strong>튜토리얼을 불러오는 중…</strong></div>}
        {tutorial && <div className="tutorial-steps">{tutorial.steps.map((step, i) => <TutorialStep key={step.id} step={step} n={i + 1}/>)}</div>}
      </section>}

      {tab === "notices" && <section className="page notices-page">
        <div className="welcome-strip"><div><span className="live-dot"/>SLACK · {announcements?.channel || "공지"}</div><p>{announcements?.ok === false ? "공지를 불러오지 못했습니다." : "관리자 공지입니다. 노드 점검·정책 변경 등을 확인하세요."}</p><button onClick={loadAnnouncements}>새로고침 <Icon name="refresh" size={15}/></button></div>
        {!announcements && <div className="empty-table"><Icon name="bell" size={28}/><strong>공지를 불러오는 중…</strong></div>}
        {announcements?.ok === false && <div className="notice-error"><Icon name="warn" size={18}/><p>{announcements.message || "Slack 공지를 읽지 못했습니다."}</p></div>}
        {announcements?.announcements?.length > 0 && <div className="notice-list">{announcements.announcements.map((a) => <AnnouncementCard key={a.ts} a={a}/>)}</div>}
        {announcements?.ok && !announcements.announcements?.length && <div className="empty-table"><Icon name="bell" size={28}/><strong>공지가 없습니다</strong><span>새 공지가 올라오면 여기에 표시됩니다.</span></div>}
      </section>}
    </main>

    {health && !health.seraph_reachable && <div className="connect-overlay"><div className="connect-card"><div className="connect-logo"><Icon name="server" size={30}/></div><p className="eyebrow">SERAPH CONNECTION</p><h2>서버 연결이 필요합니다</h2><p>입력한 사용자명은 SSH 로그인과 <code>/data/사용자명</code> 작업 경로에 사용합니다. 비밀번호는 저장하지 않습니다.</p>{health.mode === "ssh" && <><input autoComplete="username" placeholder="SERAPH 사용자명" value={sshUsername} onChange={(e) => setSshUsername(e.target.value)}/><div className="connect-endpoint"><input placeholder="호스트" value={sshHost} onChange={(e) => setSshHost(e.target.value)}/><input type="number" min="1" max="65535" placeholder="포트" value={sshPort} onChange={(e) => setSshPort(e.target.value)}/></div><input type="password" autoComplete="off" placeholder="SSH 비밀번호 (키 인증이면 비워 두기)" value={password} onChange={(e) => setPassword(e.target.value)} onKeyDown={(e) => e.key === "Enter" && connect()}/></>}<button className="primary full" onClick={connect} disabled={loading || (health.mode === "ssh" && (!sshUsername || !sshHost || !sshPort))}>{loading ? "연결 중…" : "SERAPH 연결"}</button></div></div>}
    {loading && <div className="loading-line"/>}
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
  const abs = t.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
  const diff = (t.getTime() - Date.now()) / 1000;
  if (diff <= 60) return { abs, rel: "곧" };
  const h = Math.floor(diff / 3600), m = Math.floor((diff % 3600) / 60);
  return { abs, rel: h > 0 ? `약 ${h}시간 ${m}분 뒤` : `약 ${m}분 뒤` };
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
      <span className="qeta">{j.estimated_start ? formatEta(j.estimated_start).abs : "—"}{j.estimated_start && j.confidence !== "medium" ? " (추정)" : ""}</span>
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
  return <div className="job-drawer">
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
  </div>;
}
