import { LockKeyhole } from "lucide-react";
import { useState } from "react";

const command = ".\\scripts\\run_local_docker.ps1 -Action Up -Mode Interactive";

export default function ReadOnlyNotice() {
  const [copied, setCopied] = useState(false);
  return <div className="notice readonly-notice" role="status">
    <LockKeyhole size={17} aria-hidden="true" />
    <div><strong>当前容器为只读保护模式</strong><p>在本机终端切换到交互模式以提交写入。</p>
      <code>{command}</code></div>
    {navigator.clipboard && <button type="button" className="button compact"
      onClick={() => void navigator.clipboard.writeText(command).then(() => setCopied(true), () => setCopied(false))}>
      {copied ? "已复制" : "复制命令"}
    </button>}
  </div>;
}
