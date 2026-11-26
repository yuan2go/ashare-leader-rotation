"""Render shadow ledger events and send them only through configured channels."""
import argparse
import csv
import json
import os
import re
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ACCOUNTS = {"DEF50", "E4M", "V5", "C3"}
ACTION_STATES = {"PROXY_ORDER", "SIGNAL_DUE_NO_PROXY_FILL"}
V5_VERDICT = Path(__file__).resolve().parents[1]/"reports/phase7c_hardening/phase7c_v5_placebo_verdict.json"
SECRET_ENV = (
    "TUSHARE_TOKEN", "TUSHARE_HTTP_URL", "PHASE7A_BASIC_AUTH_PASS",
    "SHADOW_SMTP_PASS", "SHADOW_NOTIFY_WEBHOOK_URL",
)


def redactor(env):
    secrets = sorted({env[name] for name in SECRET_ENV if env.get(name)}, key=len, reverse=True)
    pattern = re.compile("|".join(map(re.escape, secrets))) if secrets else None

    def redact(value):
        return pattern.sub("[REDACTED]", value) if pattern else value

    return redact


def read_object(path):
    try:
        status = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return status if isinstance(status, dict) else {}


def action_rows(directory):
    try:
        with (directory/"next_open_order_ticket.csv").open(encoding="utf-8-sig", newline="") as stream:
            return [row for row in csv.DictReader(stream)
                    if row.get("account") in ACCOUNTS and row.get("state") in ACTION_STATES]
    except FileNotFoundError:
        return []


def decide(args, env):
    redact = redactor(env)
    status = read_object(args.status_dir/"last_collection_status.json")
    state = status.get("state")
    kind = None
    details = []
    if args.exit_code not in (0, 10) or state == "collection_error":
        kind = "ERROR"
    elif args.exit_code == 10 or state == "pending_upstream_publication":
        if args.notify_on_pending:
            kind = "PENDING"
    else:
        orders = action_rows(args.status_dir)
        if orders:
            kind = "ACTION_DUE"
        v5_observation = (any(row["account"] == "V5" for row in orders)
                          and read_object(V5_VERDICT).get("verdict") == "UNVERIFIED_OBSERVATION")
        for row in orders:
            if v5_observation and row["account"] == "V5":
                row = {**row, "risk": "OBSERVATION · 波动率待验"}
            fields = ("account", "risk", "state", "side", "code", "name")
            label = " ".join(row[key] for key in fields if row.get(key))
            if row.get("proxy_price_rmb") and row.get("shares"):
                label += f" | 代理价 ¥{row['proxy_price_rmb']} | {row['shares']} 股"
            details.append(label)
    if kind in ("ERROR", "PENDING"):
        reason = status.get("reason")
        if isinstance(reason, str) and reason.strip():
            reason = redact(reason)
            details.append(" ".join(re.sub(r"https?://\S+", "[REDACTED]", reason, flags=re.I).split())[:300])
        else:
            details.append("采集或入账进程未提供原因")
    if not kind and args.always:
        kind = "STATUS"
        details.append("测试通知")
    title = redact(f"[{kind}] {args.date} {args.action}") if kind else None
    message = redact("\n".join([title, f"exit_code={args.exit_code}", *details])) if title else None
    channels = [channel for channel, key in (
        ("webhook", "SHADOW_NOTIFY_WEBHOOK_URL"), ("smtp", "SHADOW_SMTP_HOST")
    ) if env.get(key)]
    return dict(notify=bool(kind), event=kind, title=title, message=message,
                channels=channels, dry_run=args.dry_run)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_webhook(url, decision):
    destination = urlsplit(url)
    if destination.scheme not in ("http", "https") or not destination.hostname:
        raise ValueError("Invalid notification webhook URL")
    if destination.hostname == "open.feishu.cn":
        payload = {"msg_type": "text", "content": {"text": decision["message"]}}
    else:
        payload = {key: decision[key] for key in ("event", "title", "message")}
    request = Request(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    with build_opener(NoRedirects()).open(request, timeout=10) as response:
        if destination.hostname == "open.feishu.cn":
            result = json.loads(response.read())
            if result.get("code", result.get("StatusCode")) != 0:
                raise ValueError("Notification webhook rejected the message")


def send_smtp(env, decision):
    host, port = env["SHADOW_SMTP_HOST"], int(env.get("SHADOW_SMTP_PORT") or "587")
    user, password = env.get("SHADOW_SMTP_USER"), env.get("SHADOW_SMTP_PASS")
    if bool(user) != bool(password):
        raise ValueError("SMTP user and password must be configured together")
    message = EmailMessage()
    message["Subject"] = decision["title"]
    message["From"] = env["SHADOW_SMTP_FROM"]
    message["To"] = env["SHADOW_SMTP_TO"]
    message.set_content(decision["message"])
    ssl_context = ssl.create_default_context()
    connection = (smtplib.SMTP_SSL(host, port, timeout=10, context=ssl_context) if port == 465
                  else smtplib.SMTP(host, port, timeout=10))
    with connection as smtp:
        if port != 465:
            smtp.starttls(context=ssl_context)
        if user:
            smtp.login(user, password)
        if smtp.send_message(message):
            raise smtplib.SMTPException("SMTP refused a recipient")


def notification_date(value):
    day = date.fromisoformat(value).isoformat()
    if day != value:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD")
    return day


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-dir", required=True, type=Path)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--action", required=True)
    parser.add_argument("--date", required=True, type=notification_date)
    for flag in ("notify-on-pending", "always", "dry-run"):
        parser.add_argument(f"--{flag}", action="store_true")
    args = parser.parse_args(argv)
    decision = decide(args, os.environ)
    failures = []
    if decision["notify"] and not args.dry_run:
        for channel in decision["channels"]:
            try:
                if channel == "webhook":
                    send_webhook(os.environ["SHADOW_NOTIFY_WEBHOOK_URL"], decision)
                else:
                    send_smtp(os.environ, decision)
            except Exception as exc:
                # Provider errors can include raw addresses and credentials.
                failures.append(dict(channel=channel, error_type=type(exc).__name__))
    print(json.dumps(dict(decision, delivery_failures=failures), ensure_ascii=False))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
