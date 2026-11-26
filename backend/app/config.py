import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    shadow_dir: Path
    database: Path
    reports_dir: Path
    engine_lock: Path
    manifest: Path
    ops_lock: Path
    frontend_dist: Path
    cors_origins: tuple[str, ...] = ORIGINS
    api_read_only: bool = False
    allowed_hosts: tuple[str, ...] = LOCAL_HOSTS
    basic_auth_user: str = ""
    basic_auth_password: str = ""
    health_public: bool = False
    trust_proxy_headers: bool = False

    @classmethod
    def from_env(cls):
        def path(key, default):
            return Path(os.environ.get(key, default)).resolve()

        shadow = path("PHASE7A_DIR", ROOT/"reports/phase7a_service_shadow")
        return cls(
            shadow_dir=shadow,
            database=path("PHASE7A_DB", shadow/"shadow_v7a.sqlite3"),
            reports_dir=path("PHASE7A_REPORTS",ROOT/"reports"),
            engine_lock=path("PHASE7A_ENGINE_LOCK",ROOT/"engine_integrity.json"),
            manifest=path("PHASE7A_MANIFEST",shadow/"phase7a_manifest.json"),
            ops_lock=path("PHASE7A_OPS_LOCK",shadow/"ops"/"execution.lock"),
            frontend_dist=path("PHASE7A_FRONTEND_DIST",ROOT/"frontend/dist"),
            cors_origins=tuple(
                x.strip() for x in os.environ.get("PHASE7A_CORS_ORIGINS",",".join(ORIGINS)).split(",") if x.strip()
            ),
            api_read_only=env_flag("PHASE7A_API_READ_ONLY") or env_flag("API_READ_ONLY"),
            allowed_hosts=tuple(
                host.strip().lower()
                for host in os.environ.get("PHASE7A_ALLOWED_HOSTS", ",".join(LOCAL_HOSTS)).split(",")
                if host.strip()
            ),
            basic_auth_user=os.environ.get("PHASE7A_BASIC_AUTH_USER", ""),
            basic_auth_password=os.environ.get("PHASE7A_BASIC_AUTH_PASS", ""),
            health_public=env_flag("PHASE7A_HEALTH_PUBLIC"),
            trust_proxy_headers=env_flag("PHASE7A_TRUST_PROXY_HEADERS"),
        )
