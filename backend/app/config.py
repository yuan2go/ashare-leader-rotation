import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


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
        )
