import { useKline } from "../state";

export default function SecurityCode({code, name}: {code: string | null | undefined; name?: string | null}) {
  const open = useKline();
  return code && /^[0-9]{6}\.(SH|SZ)$/.test(code)
    ? <button type="button" className="security-code" onClick={(event) => {
        event.stopPropagation();
        open(code);
      }} aria-label={`查看 ${code}${name ? " " + name : ""} K 线`}>
        <span className="mono">{code}</span>{name && <strong>{name}</strong>}
      </button>
    : <span className="mono">{code || "—"}</span>;
}
