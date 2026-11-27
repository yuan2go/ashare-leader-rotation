import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleCheck, Play, ShieldAlert, Terminal } from "lucide-react";
import { useEffect, useState } from "react";
import ReadOnlyNotice from "../components/ReadOnlyNotice";
import type { FormEvent } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { collectionQuery, healthQuery } from "../api/queries";
import { DataTable, Empty, PageHeading, Panel, QueryNotice, Select } from "../components/ui";
import type { Column } from "../components/ui";
import { integer, text, timestamp } from "../utils/format";
import { collectionLabel, provenanceLabel } from "../utils/labels";

const actionOptions = [
  ["Ingest", "离线入账"], ["VerifyOffline", "离线验收"],
  ["Collect", "行情采集"], ["DailyAll", "采集并入账"],
] as const satisfies readonly (readonly [Schema["Action"], string])[];
const jobLabels: Record<Schema["Job"]["state"], string> = {
  queued: "已排队", running: "执行中", succeeded: "已完成", failed: "失败", interrupted: "已中断",
};

const activeJob = (job: Schema["Job"] | undefined) => job?.state === "queued" || job?.state === "running";
const onlineAction = (action: Schema["Action"]) => action === "Collect" || action === "DailyAll";

const collectionColumns: Column<Schema["CollectionStatus"]>[] = [
  {title: "交易日", render: (row) => <strong className="mono">{row.day}</strong>},
  {title: "采集状态", render: (row) => <span className={`badge ${row.state === "pending_upstream_publication" ? "risk-observer" : "risk-benchmark"}`}>
    {collectionLabel[row.state] ?? row.state}</span>},
  {title: "来源", render: (row) => <div className="cell-stack"><span>{row.source_profile}</span>
    <span className="muted">{row.provenance_mode ? provenanceLabel[row.provenance_mode] : "—"}</span></div>},
  {title: "更新时间", render: (row) => <span className="mono">{timestamp(row.updated_at)}</span>},
  {title: "行情请求数", numeric: true, render: (row) => integer(row.market_http_requests)},
  {title: "状态明细", render: (row) => <div className="cell-stack wrap-cell"><span>{text(row.reason)}</span>
    {row.factor_staging && <span className="muted">{row.factor_staging}</span>}</div>},
];

function JobBadge({job}: {job: Schema["Job"]}) {
  const tone = job.state === "succeeded" ? "risk-candidate"
    : job.state === "failed" || job.state === "interrupted" ? "risk-observer" : "risk-volcap";
  return <span className={`badge ${tone}`}>{jobLabels[job.state]}</span>;
}

function IntegrityPanel() {
  const health = useQuery(healthQuery);
  const integrity = useQuery({queryKey: ["health", "integrity"], queryFn: ({signal}) => api.integrity(signal),
    staleTime: 5 * 60_000, refetchOnWindowFocus: false});
  const report = integrity.data;
  return <Panel title="系统完整性"
    actions={<button type="button" className="button compact" disabled={integrity.isFetching}
      onClick={() => void integrity.refetch()}>核对哈希</button>}>
    <QueryNotice query={integrity} />
    <QueryNotice query={health} />
    {report && <div className={`integrity-state ${report.ok ? "accent" : "warning-text"}`}>
      {report.ok ? <CircleCheck size={19} aria-hidden="true" /> : <ShieldAlert size={19} aria-hidden="true" />}
      <strong>{report.ok ? "签发文件校验通过" : "签发文件存在差异"}</strong>
      <span className="mono">{integer(report.checked_files)} 个文件</span>
    </div>}
    <dl className="detail-list">
      <div><dt>API 版本</dt><dd>{text(health.data?.api_version)}</dd></div>
      <div><dt>Python</dt><dd>{text(health.data?.python_version)}</dd></div>
      <div><dt>SQLite</dt><dd>{text(health.data?.sqlite_version)}{health.data?.sqlite_readonly && " · 只读"}</dd></div>
    </dl>
    {report && <details className="inline-details hash-list"><summary>冻结引擎 SHA-256</summary>
      {Object.entries(report.frozen_sha256).map(([path, hash]) => <div key={path}><strong>{path}</strong><code>{hash}</code></div>)}
      {report.missing_files.map((path) => <p className="warning-text" key={path}>缺失 {path}</p>)}
      {report.changed_files.map((path) => <p className="warning-text" key={path}>摘要变更 {path}</p>)}
    </details>}
  </Panel>;
}

export default function OpsPage() {
  const client = useQueryClient();
  const readOnly = useQuery(healthQuery).data?.api_read_only === true;
  const [action, setAction] = useState<Schema["Action"]>("Ingest");
  const [day, setDay] = useState("");
  const [allowHttp, setAllowHttp] = useState(false);
  const [selected, setSelected] = useState("");
  const collection = useQuery(collectionQuery);
  const jobs = useQuery({queryKey: ["ops", "jobs"], queryFn: ({signal}) => api.jobs(signal),
    refetchInterval: (query) => query.state.data?.some(activeJob) ? 1500 : 15_000});
  const job = useQuery({
    queryKey: ["ops", "job", selected], queryFn: ({signal}) => api.job(selected, signal),
    enabled: !!selected, refetchInterval: (query) => activeJob(query.state.data) ? 1500 : false,
  });
  const jobLog = useQuery({
    queryKey: ["ops", "log", selected], queryFn: ({signal}) => api.jobLog(selected, signal),
    enabled: !!selected, refetchInterval: activeJob(job.data) ? 1800 : false,
  });
  const submit = useMutation({
    mutationFn: api.submitJob,
    onSuccess: (result) => {
      client.setQueryData(["ops", "job", result.id], result);
      setSelected(result.id);
      setAllowHttp(false);
      void client.invalidateQueries({queryKey: ["ops", "jobs"]});
    },
  });
  const state = job.data?.state;
  useEffect(() => {
    if (!state || state === "queued" || state === "running") return;
    void client.invalidateQueries({queryKey: ["ops", "log", selected]});
    void client.invalidateQueries({queryKey: ["ops", "jobs"]});
    if (state === "succeeded") {
      for (const key of ["shadow", "tickets"]) void client.invalidateQueries({queryKey: [key]});
      void client.invalidateQueries({queryKey: collectionQuery.queryKey});
    }
  }, [client, selected, state]);

  const online = onlineAction(action);
  const busy = submit.isPending || jobs.data?.some(activeJob) || activeJob(job.data);
  const allowed = !readOnly && !busy && (!online || allowHttp);

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!allowed) return;
    submit.mutate(online ? {action, day: day || null, allow_market_http: true} : {action, allow_market_http: false});
  }

  return <div className="page">
    <PageHeading title="受控运维与系统完整性" description="查看采集状态、后台任务日志和冻结文件核对结果" />
    <div className="ops-grid">
      <Panel title="发起运维任务" description="任务由本地 API 排队执行">
        {readOnly && <ReadOnlyNotice />}
        <form className="ops-form" onSubmit={onSubmit}>
          <Select label="任务类型" value={action} options={actionOptions} onChange={(value) => {
            setAction(value); setAllowHttp(false); submit.reset();
          }} />
          {online && <>
            <label className="field"><span>采集交易日</span><input type="date" value={day}
              onChange={(event) => setDay(event.currentTarget.value)} /></label>
            <label className="check-field online-check"><input type="checkbox" checked={allowHttp}
              onChange={(event) => setAllowHttp(event.currentTarget.checked)} />允许本次任务访问行情服务</label>
          </>}
          {busy && <span className="muted" role="status">已有任务正在处理</span>}
          {submit.isError && <div role="alert" className="notice error-notice">{errorMessage(submit.error)}</div>}
          <button type="submit" className="button primary" disabled={!allowed}>
            <Play size={15} aria-hidden="true" />{submit.isPending ? "提交中" : "提交任务"}
          </button>
        </form>
      </Panel>
      <IntegrityPanel />
    </div>
    <Panel title="近期任务">
      <QueryNotice query={jobs} />
      {jobs.data && <DataTable label="近期运维任务" rows={jobs.data} rowKey={(row) => row.id} empty="暂无任务记录" columns={[
        {title: "任务", render: (row) => <button type="button" className="text-button" aria-pressed={row.id === selected}
          onClick={() => setSelected(row.id)}>{actionOptions.find(([key]) => key === row.request.action)?.[1] ?? row.request.action}</button>},
        {title: "状态", render: (row) => <JobBadge job={row} />},
        {title: "提交时间", render: (row) => <span className="mono">{timestamp(row.created_at)}</span>},
        {title: "完成时间", render: (row) => <span className="mono">{timestamp(row.finished_at)}</span>},
        {title: "退出码", numeric: true, render: (row) => integer(row.exit_code)},
      ]} />}
      {selected && <div className="job-log">
        <div className="risk-title"><Terminal size={16} aria-hidden="true" /><h3>任务日志</h3>
          {job.data && <JobBadge job={job.data} />}</div>
        <QueryNotice query={job} />
        <QueryNotice query={jobLog} />
        <span className="mono muted job-id">{selected}</span>
        {jobLog.data?.truncated && <p className="muted">日志仅显示末尾片段。</p>}
        {jobLog.data?.text ? <pre tabIndex={0} aria-label="运维日志文本">{jobLog.data.text}</pre>
          : jobLog.data && <Empty label="该任务暂无日志输出" />}
      </div>}
    </Panel>
    <Panel title="采集状态" description="请求数与更新时间来自对应的原始采集记录">
      <QueryNotice query={collection} />
      {collection.data && <DataTable label="采集状态" rows={collection.data} columns={collectionColumns} rowKey={(row) => row.day} />}
    </Panel>
  </div>;
}
