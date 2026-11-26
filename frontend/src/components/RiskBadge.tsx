import { TriangleAlert } from "lucide-react";
import type { Schema } from "../api/client";

export default function RiskBadge({role, label}: {role: Schema["Role"]; label: string}) {
  const observer = label.includes("OBSERVATION") || role === "exploratory_observer_only";
  const kind = observer ? "observer" : role.startsWith("benchmark") ? "benchmark"
    : role === "is_locked_volcap" ? "volcap" : "candidate";
  return <span className={`badge risk-${kind}`}>
    {observer && <TriangleAlert size={13} aria-hidden="true" />}{label}
  </span>;
}
