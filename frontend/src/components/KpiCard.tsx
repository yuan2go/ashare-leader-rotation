import type { LucideIcon } from "lucide-react";

export default function KpiCard({label, value, note, icon: Icon, tone = ""}: {
  label: string; value: string; note: string; icon: LucideIcon; tone?: string;
}) {
  return <div className="kpi-card">
    <div className="kpi-label"><span>{label}</span><Icon size={16} strokeWidth={1.65} aria-hidden="true" /></div>
    <strong className={`kpi-value mono ${tone}`}>{value}</strong>
    <span className="kpi-note">{note}</span>
  </div>;
}
