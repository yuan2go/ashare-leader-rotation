"""Start and verify a loopback Docker cockpit against an independent runtime copy."""

import argparse
import base64
from contextlib import closing
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.integrity import require_runtime_guard
from research.v2.audit import digest, now, write_json
from scripts.bundle_deploy_data import SIGNED_LEDGER, runtime_sources, safe_path
from scripts.fetch_phase9b_macro_xasset import snapshot
from tushare_client import local_settings

ADDRESS = "http://127.0.0.1:8000/"
ACCEPTANCE = ROOT / "reports/phase11_workbench/docker_acceptance.json"
PROJECT = "ashare-phase9b-local"
HEALTH = "/api/v1/health"
OVERVIEW = "/api/v1/shadow/overview"
DEFAULTS = dict(PHASE7A_ALLOWED_HOSTS="localhost,127.0.0.1,[::1]", PHASE7A_API_READ_ONLY="1",
                PHASE7A_HEALTH_PUBLIC="1", PHASE7A_SQLITE_WAL="1", PHASE7A_TRUST_PROXY_HEADERS="0",
                PHASE7A_UVICORN_FORWARDED_ALLOW_IPS="")
PROTECTED_KEYS = (*DEFAULTS, "TUSHARE_TOKEN", "TUSHARE_HTTP_URL", "PHASE11_MARKET_PORT", "PHASE11_AUTO_REFRESH", "SHADOW_NOTIFY_WEBHOOK_URL",
                  "SHADOW_SMTP_HOST", "SHADOW_SMTP_USER", "SHADOW_SMTP_PASS", "SHADOW_SMTP_FROM", "SHADOW_SMTP_TO")


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def no_link(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        check(not path.is_symlink() and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
              "本地运行目录及文件拒绝符号链接或重解析点")
        if path.is_file():
            check(info.st_nlink == 1, "本地运行库或配置拒绝硬链接")


def runtime_path(root):
    parent = root / ".docker_runtime"
    target = parent / "shadow"
    no_link(parent)
    no_link(target)
    check(target.resolve() == target, "运行目录解析后超出本地 Docker 隔离范围")
    return target


def copy_runtime(root, signed_sha):
    target = runtime_path(root)
    origin = safe_path(root, SIGNED_LEDGER.parent.as_posix())
    original = origin / SIGNED_LEDGER.name
    no_link(original)
    check(digest(original) == signed_sha, "原签发 SQLite SHA-256 不符")
    target.parent.mkdir(exist_ok=True, mode=0o700)
    check(target.parent.is_dir(), "运行时父目录无效")
    if not target.exists():
        with TemporaryDirectory(dir=target.parent) as temp:
            stage = Path(temp) / "shadow"
            stage.mkdir(mode=0o700)
            for name, source in [(SIGNED_LEDGER.name, original), *runtime_sources(origin)]:
                destination = safe_path(stage, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                check(digest(destination) == digest(source), "运行时副本校验失败")
            check(digest(stage / SIGNED_LEDGER.name) == signed_sha, "SQLite 副本哈希不符")
            stage.replace(target)
    check(target.is_dir(), "运行目录必须是独立目录")
    for path in target.rglob("*"):
        no_link(path)
    runtime_db = target / SIGNED_LEDGER.name
    check(runtime_db.is_file() and not runtime_db.samefile(original), "运行库与签发库身份必须独立")
    check(not any(runtime_db.with_name(runtime_db.name + suffix).samefile(original)
                  for suffix in ("-wal", "-shm") if runtime_db.with_name(runtime_db.name + suffix).exists()),
          "WAL 或 SHM 与签发库身份冲突")
    check(digest(original) == signed_sha, "准备运行副本时原库发生变化")
    return target


def local_env(root):
    path = root / ".env.docker.local"
    no_link(path)
    if not path.exists():
        lines = []
        for line in (root / ".env.example").read_text(encoding="utf-8-sig").splitlines():
            name, separator, _ = line.partition("=")
            if separator and name in (*DEFAULTS, "PHASE7A_BASIC_AUTH_USER", "PHASE7A_BASIC_AUTH_PASS", "SHADOW_RUNTIME_DIR"):
                value = DEFAULTS.get(name, "./.docker_runtime/shadow" if name == "SHADOW_RUNTIME_DIR" else "")
                lines.append(f"{name}={value}")
        check({line.split("=")[0] for line in lines} >= set(DEFAULTS), ".env.example 缺少本机安全默认值")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
    return path


def docker_candidates():
    binary = shutil.which("docker")
    if binary:
        yield (binary,)
    if os.name == "nt":
        for parent, product in (("ProgramFiles", "Docker/Docker"),
                                ("LOCALAPPDATA", "Programs/Docker/Docker"),
                                ("LOCALAPPDATA", "Programs/DockerDesktop"),
                                ("LOCALAPPDATA", "Programs/Docker")):
            if parent in os.environ:
                path = Path(os.environ[parent]) / product / "resources/bin/docker.exe"
                if path.is_file() and str(path).casefold() != (binary or "").casefold():
                    yield (str(path),)
        wsl = shutil.which("wsl")
        if wsl:
            yield (wsl, "--exec", "docker")


def docker_environment(command):
    env = os.environ.copy()
    if len(command) == 1:
        # Docker Desktop locates its registry credential helper through PATH,
        # even when docker.exe itself was discovered by absolute path.
        env["PATH"] = str(Path(command[0]).parent) + os.pathsep + env.get("PATH", "")
    return env


def probe(command):
    tools = {"docker_version": ("--version",), "compose_version": ("compose", "version"),
             "docker_info": ("info", "--format", "{{.ServerVersion}}")}
    evidence = {}
    env = docker_environment(command)
    endpoint = None if len(command) > 1 or os.environ.get("DOCKER_CONTEXT") else os.environ.get("DOCKER_HOST")
    if not endpoint:
        try:
            context = subprocess.run(
                [*command, "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}'],
                capture_output=True, encoding="utf-8", errors="replace", timeout=10, env=env)
            endpoint = context.stdout.strip() if context.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            endpoint = ""
    local = bool(endpoint and endpoint.startswith(("unix:///", "npipe:////./pipe/", "npipe://./pipe/")))
    for key, args in tools.items():
        if key == "docker_info" and not local:
            evidence[key] = dict(exit_code=125, reason="docker_context_is_not_a_local_socket")
            continue
        try:
            result = subprocess.run([*command, *args], capture_output=True, encoding="utf-8", errors="replace",
                                    timeout=45 if key == "docker_info" else 15, env=env)
            evidence[key] = dict(exit_code=result.returncode)
            if result.returncode == 0:
                evidence[key]["version"] = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            evidence[key] = dict(exit_code=125, reason="docker_probe_failed_or_timed_out")
    return evidence


def discover():
    attempts = []
    for command in dict.fromkeys(docker_candidates()):
        evidence = probe(command)
        attempts.append(dict(cli=command[0], wsl=len(command) > 1, checks=evidence))
        if all(row["exit_code"] == 0 for row in evidence.values()):
            return command, True, attempts
    return None, False, attempts or [dict(reason="docker_cli_not_found")]


def stage_build_inputs(root, runtime):
    """Copy only hashes needed by offline jobs from directories omitted by .dockerignore."""
    target = runtime.parent / "build_inputs"
    no_link(target)
    target.mkdir(exist_ok=True)
    manifest = json.loads((runtime / "phase7a_manifest.json").read_bytes())
    prefixes = ("data/pit_csi300/compiled/", "reports/phase5a_multi_shadow/",
                "reports/phase6a_vol_and_pit_probe/")
    files = {p: h for p, h in manifest["protected_before"].items() if p.startswith(prefixes)}
    cross = root / "data/pit_phase9b/compiled/pit_xasset_manifest.json"
    study = root / "reports/phase10_multialpha/phase10_manifest.json"
    for source in (cross, study):
        files[source.relative_to(root).as_posix()] = digest(source)
    for name, expected in json.loads(cross.read_bytes())["compiled_sha256"].items():
        files[f"data/pit_phase9b/compiled/{name}"] = expected
    phase10 = json.loads(study.read_bytes())
    files["reports/phase9b_xasset_and_docker/phase9b_summary.csv"] = phase10["phase9b_stage2_artifact_sha256"]["phase9b_summary.csv"]
    for name in ("phase10_factor_court.csv", "phase10_verdict.json", "phase10_vpb_summary.csv",
                 "phase10_blend_summary.csv", "phase10_corr_matrix.csv"):
        files[f"reports/phase10_multialpha/{name}"] = phase10["artifacts_sha256"][name]
    for name, expected in files.items():
        origin = safe_path(root, name)
        check(digest(origin) == expected, f"构建证据哈希不符 {name}")
        destination = safe_path(target, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        no_link(destination)
        if not destination.is_file() or digest(destination) != expected:
            shutil.copyfile(origin, destination)
        check(digest(destination) == expected, f"构建证据副本哈希不符 {name}")
    return target


def mount_guard(config, target, mode="ReadOnly"):
    services = config["services"]
    cockpit = services["cockpit"]
    ports = cockpit["ports"]
    check(len(ports) == 1 and ports[0].get("host_ip") == "127.0.0.1"
          and str(ports[0].get("published")) == "8000" and ports[0].get("target") == 8000,
          "Compose 端口必须仅绑定 127.0.0.1:8000")
    for service in ("cockpit", "shadow-daily", "shadow-volume-init", "worker"):
        if service not in services and service == "worker":
            continue
        row = services[service]
        writable = [v for v in row["volumes"] if not v.get("read_only", False)]
        check(len(writable) == 1, "每个服务只能挂载一处可写运行卷")
        entry = writable[0]
        check(entry["type"] == "bind" and entry["target"] == "/data/shadow"
              and Path(entry["source"]).resolve() == target, "拒绝挂载未核准的可写目录")
    check(str(cockpit["environment"]["PHASE7A_API_READ_ONLY"]) == ("1" if mode == "ReadOnly" else "0")
          and str(cockpit["environment"]["PHASE7A_HEALTH_PUBLIC"]) == "1"
          and str(services["shadow-daily"]["environment"]["PHASE7A_SQLITE_WAL"]) == "1",
          "本机写入模式、公开探活或离线任务 WAL 配置不符")
    if mode == "ReadOnly":
        check(not any(cockpit["environment"].get(k) for k in ("TUSHARE_TOKEN", "TUSHARE_HTTP_URL", "PHASE11_MARKET_PORT")),
              "只读容器不得获得行情凭据")
    check(str(cockpit["environment"].get("PHASE11_AUTO_REFRESH", "0")) == ("1" if mode == "Interactive" else "0"),
          "自动更新须与本机交互模式一致")


def request(path, method="GET", credentials=None, payload=None, write=False, timeout=5):
    check(path.startswith("/") and not path.startswith("//"), "HTTP 探活仅接受本机路径")
    headers = {}
    if credentials:
        headers["Authorization"] = "Basic " + base64.b64encode(":".join(credentials).encode()).decode()
    body = None
    if write:
        headers["X-Shadow-Ops"] = "1"
    if method == "POST":
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload or {})
    with closing(HTTPConnection("127.0.0.1", 8000, timeout=timeout)) as connection:
        connection.request(method, path, body, headers)
        with connection.getresponse() as response:
            return response.status, response.headers.get("Content-Type", "").split(";")[0], response.read()


class Docker:
    def __init__(self, root, command, target, env_file, mode="ReadOnly"):
        self.root, self.command = root, command
        self.mode = mode
        self.target = target
        self.market_port = None
        self.wsl = len(command) > 1

        def path_arg(path):
            if not self.wsl:
                return str(path)
            return subprocess.check_output([command[0], "--exec", "wslpath", "-a", str(path)],
                                           cwd=root, encoding="utf-8").strip()
        self.env = docker_environment(command)
        self.env.update({k: "" for k in PROTECTED_KEYS})
        self.env.update(DEFAULTS, SHADOW_RUNTIME_DIR=path_arg(target),
                        SHADOW_LOCAL_BUILD_INPUTS=path_arg(target.parent / "build_inputs"),
                        PHASE9B_OPERATOR_GID=str(os.getgid() if hasattr(os, "getgid") else 10001))
        if mode == "Interactive":
            self.env["PHASE7A_API_READ_ONLY"] = "0"
            self.env["PHASE11_AUTO_REFRESH"] = "1"
            settings = local_settings(root)
            if all(settings.values()):
                try:
                    endpoint = urlsplit(settings["TUSHARE_HTTP_URL"])
                    valid = endpoint.scheme == "https" and endpoint.hostname and not endpoint.username
                    self.market_port = endpoint.port or 443
                except ValueError:
                    valid = False
                check(valid, "Tushare 行情端点须使用 HTTPS")
                self.env.update(settings, PHASE11_MARKET_PORT=str(self.market_port))
        # An ambient Compose file/project/profile cannot change the local deployment.
        for key in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES"):
            self.env.pop(key, None)
        self.prefix = [*command, "compose", "--project-directory", path_arg(root), "--project-name", PROJECT,
                       "--env-file", path_arg(env_file), "-f", path_arg(root / "deploy/docker-compose.local.yml")]
        self.evidence = {}
        config = json.loads(self.compose("--profile", "jobs", "config", "--format", "json", capture=True))
        for key in ("cockpit", "shadow-daily", "shadow-volume-init"):
            volumes = config["services"][key]["volumes"]
            check(len(volumes) == 1, "本机服务只允许隔离卷")
            if self.wsl:
                check(volumes[0]["source"] == self.env["SHADOW_RUNTIME_DIR"], "WSL 隔离卷路径不符")
                volumes[0]["source"] = str(target)
        check(config["networks"]["local_ingress"]["driver"] == "bridge"
              and set(config["services"]["cockpit"]["networks"]) == {"local_ingress"}
              and config["services"]["cockpit"]["entrypoint"] == ["bash", "deploy/run_local_cockpit.sh"]
              and config["services"]["shadow-daily"]["network_mode"] == "none", "本机容器网络隔离配置不符")
        mount_guard(config, target, mode)
        env = config["services"]["cockpit"]["environment"]
        self.auth = (env.get("PHASE7A_BASIC_AUTH_USER", ""), env.get("PHASE7A_BASIC_AUTH_PASS", ""))
        check(bool(self.auth[0]) == bool(self.auth[1]), "Basic Auth 用户和密码须成对设置")

    def invoke(self, argv, *, capture=False, env=None):
        environment = self.env if env is None else env
        if self.wsl:
            bridge = (*PROTECTED_KEYS, "SHADOW_RUNTIME_DIR", "SHADOW_LOCAL_BUILD_INPUTS", "PHASE9B_OPERATOR_GID",
                      "PHASE7A_BASIC_AUTH_USER", "PHASE7A_BASIC_AUTH_PASS")
            environment = environment | {"WSLENV": ":".join(bridge)}
        try:
            result = subprocess.run(argv, cwd=self.root, env=environment, check=False,
                                    capture_output=capture, encoding="utf-8", errors="replace")
        except OSError:
            raise RuntimeError("本机 Docker 命令无法启动") from None
        if result.returncode:
            # In particular, never surface expanded Compose config or credentials.
            raise RuntimeError(f"Docker 命令退出码 {result.returncode}")
        return result.stdout if capture else None

    def compose(self, *args, capture=False, env=None):
        return self.invoke([*self.prefix, *args], capture=capture, env=env)

    def wait_healthy(self):
        deadline = monotonic() + 120
        while monotonic() < deadline:
            container = self.compose("ps", "-q", "cockpit", capture=True).strip()
            if container:
                health = self.invoke([*self.command, "inspect", "--format", "{{.State.Health.Status}}", container],
                                     capture=True).strip()
                if health == "healthy":
                    try:
                        code, _, body = request(HEALTH)
                        if code == 200 and json.loads(body)["sqlite_readonly"] is True:
                            inspect = [*self.command, "inspect", "--format"]
                            published = json.loads(self.invoke([*inspect, "{{json .NetworkSettings.Ports}}", container],
                                                               capture=True))
                            check(published == {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]},
                                  "运行容器端口与回环约束不符")
                            rules = self.compose("exec", "-T", "-u", "0", "cockpit",
                                                 "iptables", "-S", "OUTPUT", capture=True).splitlines()
                            expected = ["-P OUTPUT DROP", "-A OUTPUT -m conntrack --ctstate ESTABLISHED -j ACCEPT",
                                        "-A OUTPUT -d 127.0.0.1/32 -o lo -p tcp -m tcp --dport 8000 -j ACCEPT"]
                            if self.market_port is not None:
                                expected.extend([f"-A OUTPUT -d 127.0.0.11/32 -p {p} -m conntrack --ctorigdstport 53 -j ACCEPT"
                                                 for p in ("udp", "tcp")])
                                expected.append(f"-A OUTPUT -p tcp -m tcp --dport {self.market_port} -j ACCEPT")
                            check(rules == expected, "运行容器外连规则不符")
                            check(json.loads(body)["api_read_only"] == (self.mode == "ReadOnly"), "API 运行模式不符")
                            disabled = self.compose("exec", "-T", "cockpit", "cat",
                                                    "/proc/sys/net/ipv6/conf/all/disable_ipv6", capture=True).strip()
                            process = self.compose("exec", "-T", "cockpit", "cat", "/proc/1/status", capture=True)
                            check(disabled == "1" and re.search(r"(?m)^Uid:\s+10001\s+10001\s+10001\s+10001$", process)
                                  and re.search(r"(?m)^CapEff:\s+0+$", process),
                                  "运行容器 IPv6 隔离、用户或进程权限不符")
                            self.evidence["compose_healthy"] = True
                            self.evidence["listener"] = "127.0.0.1:8000"
                            self.evidence["egress_isolation"] = dict(ipv4_output_rules=rules, ipv6_disabled=True,
                                                                      cockpit_uid=10001, networkless_jobs=True)
                            return
                    except (OSError, ValueError, KeyError):
                        pass
            sleep(2)
        raise RuntimeError("cockpit 未在 120 秒内通过容器与本机探活")

    def up(self, no_cache=False, build=True):
        if build:
            args = ("--no-cache",) if no_cache else ()
            self.compose("build", *args, "cockpit")
            self.evidence["compose_build_exit_code"] = 0
        self.compose("--profile", "jobs", "run", "--rm", "--no-deps", "shadow-volume-init")
        self.compose("up", "-d", "--no-build", "cockpit")
        self.evidence["compose_up_exit_code"] = 0
        self.wait_healthy()

    def smoke(self):
        self.wait_healthy()
        credentials = self.auth if self.auth[0] else None
        asset = None
        routes = ("/", "/tickets", "/portfolio", "/research", "/ops", "/universe", "/radar", "/wallet")
        for route in routes:
            code, mime, body = request(route, credentials=credentials)
            check(code == 200 and mime == "text/html", f"SPA 深链失败 {route}")
            link = re.search(rb'<script\b[^>]*\bsrc=["\'](/[^"\']+)["\']', body)
            check(link is not None, f"SPA 构建入口缺失 {route}")
            if asset is None:
                asset = link.group(1).decode()
            check(link.group(1).decode() == asset, "SPA 深链与首页脚本不一致")
        js = request(asset, credentials=credentials)
        check(js[:2] == (200, "text/javascript"), "JS 模块 MIME 不符")
        check(request("/api/v1/missing", credentials=credentials)[0] == 404, "API 未知路由未返回 404")
        self.evidence["spa_deep_links"] = dict(status=200, routes=routes, js_content_type=js[1], missing_api_status=404)
        code, _, body = request(OVERVIEW, credentials=credentials)
        check(code == 200, "驾驶舱概览请求失败")
        accounts = json.loads(body)["accounts"]
        v5 = [row for row in accounts if row["account"] == "V5"]
        check(len(accounts) == 12 and len(v5) == 2 and
              all(r["risk_label"] == "OBSERVATION · 波动率待验" for r in v5), "账户数量或 V5 水印不符")
        code, _, body = request("/api/v1/research/placebos?phase=6a&window=OOS", credentials=credentials)
        check(code == 200 and any(row["source"] == "phase7c_addendum" and str(row["key"]).startswith("V5")
                                 for row in json.loads(body)), "缺少 Phase 7C V5 OOS 补验")
        self.evidence["core_api"] = dict(sqlite_readonly=True, accounts=12, v5_labels=len(v5), phase7c_addendum=True)
        self.workbench_smoke(credentials)
        if self.mode == "Interactive":
            return
        check(request("/api/v1/ops/jobs", method="POST", credentials=credentials)[0] == 403, "只读运维端点未返回 403")
        self.evidence["readonly_post_status"] = 403
        temp_auth = ("phase9b_smoke", secrets.token_urlsafe(24))
        temp_env = self.env | dict(PHASE7A_BASIC_AUTH_USER=temp_auth[0], PHASE7A_BASIC_AUTH_PASS=temp_auth[1])
        try:
            self.compose("up", "-d", "--no-deps", "--force-recreate", "cockpit", env=temp_env)
            self.wait_healthy()
            codes = tuple(request(route)[0] for route in (OVERVIEW, "/", "/api/v1/missing", HEALTH))
            authenticated = request(OVERVIEW, credentials=temp_auth)[0]
            check(codes == (401, 401, 401, 200) and authenticated == 200, "Basic Auth 或公开探活行为不符")
            self.evidence["basic_auth"] = dict(unauthenticated=codes[0], authenticated=authenticated,
                                               public_health=codes[-1], protected_spa=codes[1], protected_missing=codes[2])
        finally:
            self.compose("up", "-d", "--no-deps", "--force-recreate", "cockpit")
            self.wait_healthy()
        prefix = ("--profile", "jobs", "run", "--rm", "--no-deps")
        self.compose(*prefix, "--entrypoint", "python", "shadow-daily", "-B", "/app/deploy/local_docker.py", "--probe-mounted-volume")
        for name in ("Healthcheck", "VerifyOffline"):
            output = self.compose(*prefix, "shadow-daily", name, capture=True)
            result = json.loads(output)
            check(result["market_http_requests"] == 0, "离线容器任务发生行情请求")
            self.evidence[name] = result
        self.evidence["nonroot_volume"] = dict(healthcheck_exit_code=0, verify_offline_exit_code=0,
                                               concurrent_wal_probe_exit_code=0, expected_uid=10001)

    def workbench_smoke(self, credentials):
        def get(path):
            code, _, body = request("/api/v1/" + path, credentials=credentials, timeout=60)
            check(code == 200, f"工作台 API 请求失败 {path}")
            return json.loads(body)

        universe = get("market/universe")
        kline = get("market/kline/600015.SH?limit=240")
        radar = get("market/radar")
        corr = get("market/correlation")
        archive = get("research/multialpha")
        check(260 <= len(universe["items"]) <= 300 and len(kline["bars"]) == 240
              and len(radar["items"]) == 8 and any(row["code"] == "513100.SH" for row in radar["quarantined"])
              and len(corr["matrix"]) == len(corr["codes"]) > 0
              and all(len(row) == len(corr["codes"]) for row in corr["matrix"])
              and archive["phase9b"] and archive["factor_court"] and archive["blends"]
              and archive["vpb_verdict"]["status"] == "REJECT_ARCHIVE_ONLY", "工作台真实数据验收失配")
        self.evidence["workbench_api"] = dict(universe=len(universe["items"]), as_of=universe["as_of"],
                                             kline_bars=len(kline["bars"]), radar_etfs=len(radar["items"]),
                                             correlation_dimension=len(corr["codes"]), phase9b=len(archive["phase9b"]),
                                             factor_rows=len(archive["factor_court"]), vpb_status=archive["vpb_verdict"]["status"])
        summary = get("wallet/summary")
        days = {row["day"] for row in summary["points"]}
        day = next((f"1990-01-{n:02d}" for n in range(1, 32) if f"1990-01-{n:02d}" not in days), None)
        check(day is not None, "钱包验收缺少可用的临时日期")
        payload = dict(day=day, cash_rmb=1234.56, positions=[dict(code="600015.SH", shares=100, avg_cost=5.)])
        path = "/api/v1/wallet/snapshot"
        if self.mode == "ReadOnly":
            post = request(path, method="POST", credentials=credentials, payload=payload, write=True)[0]
            delete = request(f"{path}/{day}", method="DELETE", credentials=credentials, write=True)[0]
            check((post, delete) == (403, 403), "只读钱包必须拒绝写入和删除")
            self.evidence["wallet"] = dict(post_status=post, delete_status=delete)
            return
        before = digest(self.target / SIGNED_LEDGER.name)
        post = request(path, method="POST", credentials=credentials, payload=payload, write=True)[0]
        check(post == 200, "交互钱包写入失败")
        try:
            check(any(point["day"] == day for point in get("wallet/summary")["points"]), "临时钱包快照未保存")
        finally:
            delete = request(f"{path}/{day}", method="DELETE", credentials=credentials, write=True)[0]
        check(delete == 200 and all(point["day"] != day for point in get("wallet/summary")["points"])
              and digest(self.target / SIGNED_LEDGER.name) == before, "交互钱包删除或影子库物理隔离检查失败")
        self.evidence["wallet"] = dict(post_status=post, delete_status=delete, temporary_snapshot_cleaned=True,
                                       shadow_sqlite_before=before, shadow_sqlite_after=before)


def init_mount(mount):
    assert os.getuid() == 0 and mount == Path("/data/shadow")
    operator_gid = int(os.environ.get("PHASE9B_OPERATOR_GID", "10001"))
    for path in [mount, *mount.rglob("*")]:
        no_link(path)
        os.chown(path, 10001, operator_gid, follow_symlinks=False)
        path.chmod(0o2770 if path.is_dir() else 0o660)


def wal_probe(mount):
    assert os.getuid() == 10001 and mount == Path("/data/shadow")
    db = mount / SIGNED_LEDGER.name
    no_link(db)
    with TemporaryDirectory(dir=mount) as scratch:
        clone = Path(scratch) / "wal_probe.sqlite3"
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(clone)) as dest:
            source.backup(dest)
        probe_concurrent_wal(clone)


def probe_concurrent_wal(db):
    with closing(sqlite3.connect(db)) as writer, closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as reader:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] > 0
        writer.execute("BEGIN IMMEDIATE")
        try:
            writer.execute("CREATE TABLE phase9b_wal_probe (marker INTEGER)")
            writer.execute("INSERT INTO phase9b_wal_probe VALUES (1)")
            writer.commit()
            assert db.with_name(db.name + "-wal").stat().st_size > 0
            assert db.with_name(db.name + "-shm").is_file()
            assert reader.execute("SELECT name FROM sqlite_master WHERE name='phase9b_wal_probe'").fetchone() is None
            reader.rollback()
            assert reader.execute("SELECT marker FROM phase9b_wal_probe").fetchone() == (1,)
        finally:
            writer.rollback()
            writer.execute("DROP TABLE IF EXISTS phase9b_wal_probe")
            writer.commit()
        assert writer.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def main(argv=None):
    require_runtime_guard()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="Up", choices=("Up", "Start", "Down", "Rebuild", "Status", "Logs", "Smoke"))
    parser.add_argument("--mode", choices=("ReadOnly", "Interactive"), default="ReadOnly")
    internal = parser.add_mutually_exclusive_group()
    internal.add_argument("--init-mounted-volume", action="store_true", help=argparse.SUPPRESS)
    internal.add_argument("--probe-mounted-volume", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.init_mounted_volume or args.probe_mounted_volume:
        (init_mount if args.init_mounted_volume else wal_probe)(Path("/data/shadow"))
        return 0
    command, available, evidence = discover()
    before, sources = snapshot()
    db_sha = before[SIGNED_LEDGER.as_posix()]
    report = dict(docker_runtime_status="daemon_unavailable" if not available else "daemon_available",
                  probes=evidence, requested_action=args.action, mode=args.mode, address=None, checked_at_utc=now(),
                  signed_sqlite_before=db_sha, checks={}, status="blocked")
    if not available:
        report["not_run"] = ["multi_stage_build", "container_health", "spa_deep_links", "api_and_governance",
                             "read_only_and_basic_auth", "nonroot_wal_and_concurrent_read", "healthy_listener"]
    docker = None
    code = 2
    try:
        if available:
            env = local_env(ROOT)
            dest = runtime_path(ROOT)
            if args.action in ("Up", "Start", "Rebuild"):
                dest = copy_runtime(ROOT, db_sha)
                if args.action != "Start":
                    stage_build_inputs(ROOT, dest)
            docker = Docker(ROOT, command, dest, env, args.mode)
            if args.action in ("Up", "Start", "Rebuild"):
                docker.up(no_cache=args.action == "Rebuild", build=args.action != "Start")
            elif args.action == "Smoke":
                docker.smoke()
            elif args.action == "Down":
                docker.compose("down")
            elif args.action == "Status":
                docker.compose("ps", "--all")
            else:
                docker.compose("logs", "--tail", "100", "cockpit")
            code = 0
            report["status"] = "verified" if args.action == "Smoke" else "completed"
            if args.action in ("Up", "Start", "Rebuild", "Smoke"):
                report["address"] = ADDRESS
        else:
            print("Docker CLI、Compose 或 Daemon 不可用，容器闭环未执行。", file=sys.stderr)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        report["error"] = str(exc)
        code = 1
    finally:
        after = snapshot()
        assert after == (before, sources)
        report["signed_sqlite_after"] = after[0][SIGNED_LEDGER.as_posix()]
        report["protected_count"] = sum(v is not None for v in before.values())
        report["exit_code"] = code
        if docker:
            report["checks"] = docker.evidence
            if args.action == "Smoke" and code == 0:
                report["market_http_requests"] = 0
        if ACCEPTANCE.is_file():
            previous = json.loads(ACCEPTANCE.read_bytes())
            key = previous["mode"] + "/" + previous["requested_action"]
            report["prior_actions"] = {**previous.get("prior_actions", {}),
                                      key: {k: previous.get(k) for k in ("status", "exit_code", "checks", "market_http_requests")}}
        ACCEPTANCE.parent.mkdir(exist_ok=True, parents=True)
        write_json(ACCEPTANCE, report)
    if report["address"]:
        print(f"{ADDRESS}\nStop with scripts/run_local_docker.ps1 -Action Down")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
