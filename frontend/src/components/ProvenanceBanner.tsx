import { CircleCheck, Clock3, Database, TriangleAlert } from "lucide-react";
import type { Schema } from "../api/client";
import { collectionLabel, provenanceLabel } from "../utils/labels";
import { integer, text } from "../utils/format";

export default function ProvenanceBanner({overview, latest}: {
  overview?: Schema["Overview"]; latest?: Schema["CollectionStatus"];
}) {
  if (!overview && !latest) return null;
  const mode = overview?.latest_provenance_mode;
  const pending = latest?.state === "pending_upstream_publication";
  const catchup = mode === "post_hoc_catchup";
  const Icon = catchup || pending ? TriangleAlert : mode === "live_timely_verified" ? CircleCheck : Database;
  return <div className={`provenance-banner ${catchup || pending ? "provenance-warning" : ""}`} aria-label="账簿来源状态">
    <div className="provenance-main">
      <Icon size={17} aria-hidden="true" />
      <div>
        <strong>{mode ? provenanceLabel[mode] : "等待入账"}</strong>
        {overview && <span>最新入账 <b className="mono">{text(overview.latest_session)}</b><i />
          严格时点 <b className="mono">{integer(overview.strict_live_sessions)}</b> 日 · 补采 <b className="mono">{integer(overview.catchup_sessions)}</b> 日
        </span>}
      </div>
    </div>
    {latest && <div className="provenance-feed" title={latest.reason ?? undefined}>
      <Clock3 size={15} aria-hidden="true" /><span className="mono">{latest.day}</span>
      <strong>{collectionLabel[latest.state] ?? latest.state}</strong>
    </div>}
  </div>;
}
