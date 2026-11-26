"""Run and verify the offline Phase 9A experiment. Requires Python 3.11."""

import argparse
from itertools import product
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.integrity import require_runtime_guard
from research.v2.audit import PHASE8, PHASE8_FILES, Offline, digest, now, protected, write_json

OUT = ROOT / "reports/phase9a_v2_engine"
MANIFEST = OUT / "phase9a_manifest.json"
LOCK = OUT / "phase9a_is_selection_lock.json"
ARTIFACTS = (
    LOCK.name, "phase9a_waterfall_summary.csv", "phase9a_yearly_returns.csv",
    "phase9a_drawdown_episodes.csv", "phase9a_placebo_mc.csv", "phase9a_report.md",
)
VALIDATIONS = ("repository_tests", "v2_tests", "integrity", "openapi", "backend_tests", "frontend_tests")


def source_hashes():
    sources = [Path(__file__).resolve(), ROOT / "tests/test_phase9a_v2_engine.py",
               *sorted((ROOT / "research/v2").glob("*.py"))]
    return {path.relative_to(ROOT).as_posix(): digest(path) for path in sources}


def choose_is(study):
    from research.v2.study import BARBELLS, SELECT_FIELDS
    from research.v2.unified_book import CLOCKS

    baseline = study.summary("DEF50", 3, None, "IS", 450000)
    candidates = []
    for model, clock in product(BARBELLS, CLOCKS):
        row = study.summary(model, 3, clock, "IS", 450000)
        candidate = {name: row[name] for name in ("key", "model", "clock", *SELECT_FIELDS)}
        candidate["qualified"] = bool(row["cagr_pct"] > baseline["cagr_pct"] and row["max_dd_pct"] >= -28.)
        candidates.append(candidate)
    ordered = sorted((r for r in candidates if r["qualified"]),
                     key=lambda r: (-r["calmar"], -r["sharpe"], r["key"]))
    return dict(phase="9A", locked_at_utc=now(), window="IS", start="2016-01-04", stop="2023-01-20",
                sessions=baseline["sessions"], pool="ETF5", stock_pool="PIT300_ex688", capital=450000,
                def50_cagr_pct=baseline["cagr_pct"], min_max_dd_pct=-28.0, level="L3_REAL_FRICTION_NO_MMF",
                rank_by=["calmar_desc", "sharpe_desc", "key_asc"], candidates=candidates,
                selected=[r["key"] for r in ordered[:2]])


def run(guard):
    from research.v2.inputs import Market
    from research.v2.placebo import DRAWS, placebo
    from research.v2.reporting import core_tables, diagnose, render
    from research.v2.study import CAPITALS, SPANS, WINDOWS, Study, cells, phase6
    from research.v2.unified_book import CLOCKS, Settings, budgets

    OUT.mkdir(parents=True, exist_ok=True)
    before = protected(ROOT)
    initial_path = OUT / "phase9a_protection_before.json"
    if not initial_path.exists():
        write_json(initial_path, dict(captured_utc=now(), sha256=before))
    initial = json.loads(initial_path.read_bytes())
    assert initial["sha256"] == before
    sources = source_hashes()
    started = now()
    phase6.runtime()
    market = Market(ROOT)
    for clock in CLOCKS:
        assert sum(budgets(Settings("E4M", clock), 45_000_001, (22_800_000, 22_200_001), 1.)) == 45_000_001
    study = Study(market, ROOT)
    study.verify_anchors("IS")
    print("IS 签发锚点核验通过", len(study.anchor_checks), flush=True)
    lock = choose_is(study)
    write_json(LOCK, lock)
    lock_hash = digest(LOCK)
    selected = {r["key"]: (r["model"], r["clock"]) for r in lock["candidates"] if r["key"] in lock["selected"]}
    print("IS 选择锁已落盘", lock["selected"], flush=True)
    for window in WINDOWS:
        if window != "IS":
            study.verify_anchors(window)
    assert len(study.anchor_checks) == 56
    assert {check["sessions"] for check in study.anchor_checks if check["window"] == "W1"} == {2643}
    rows = []
    for window in SPANS:
        for capital in CAPITALS:
            rows.extend(study.summary(model, level, clock, window, capital) for model, level, clock in cells())
        print(f"{window} 两档资金回放完成", flush=True)
    assert len(rows) == len(list(cells())) * len(SPANS) * len(CAPITALS) == 544
    index, yearly, episodes = diagnose(study, rows)
    mc = placebo(study, selected)
    assert digest(LOCK) == lock_hash
    assert source_hashes() == sources
    phase6.write_csv(OUT / ARTIFACTS[1], rows)
    phase6.write_csv(OUT / ARTIFACTS[2], yearly)
    phase6.write_csv(OUT / ARTIFACTS[3], episodes)
    phase6.write_csv(OUT / ARTIFACTS[4], mc)
    (OUT / ARTIFACTS[5]).write_text(render(index, rows, mc, lock, market, study.anchor_checks), encoding="utf-8")
    after = protected(ROOT)
    assert before == after and guard.attempts == 0
    assert json.loads((OUT / ARTIFACTS[0]).read_bytes()) == lock
    write_json(MANIFEST, dict(
        phase="9A", state="offline_replay_verified", started_at_utc=started, replay_completed_at_utc=now(),
        protection_initial_captured_at_utc=initial["captured_utc"], protection_initial_sha256=digest(initial_path),
        protected_count=PHASE8_FILES, protected_before=before, protected_after=after,
        phase8_manifest_sha256=before[PHASE8], market_http_requests=guard.attempts, source_sha256=sources,
        artifact_sha256={name: digest(OUT / name) for name in ARTIFACTS},
        frozen_engine_sha256={name: after[name] for name in ("engine.py", "offline/engine.js")},
        signed_v1_regression=study.anchor_checks, market_coverage=market.coverage, units=market.units,
        test_counts={}, commands={"phase9a_runner": 0}, candidate_lock_sha256=lock_hash, lock_keys=list(selected),
        placebo_draws_each=DRAWS, matrix_rows=len(rows), yearly_rows=len(yearly), drawdown_rows=len(episodes),
        placebo_rows=len(mc), python_version=sys.version.split()[0],
    ))
    headings = ("消融瀑布", "风格周期", "真实摩擦", "安慰剂 p_upper")
    for heading, table in zip(headings, core_tables(rows, mc, selected), strict=True):
        print(f"\n## {heading}\n\n{table}", flush=True)


def finalize(guard):
    manifest = json.loads(MANIFEST.read_bytes())
    before = manifest["protected_before"]
    assert before == manifest["protected_after"] == protected(ROOT)
    assert manifest["source_sha256"] == source_hashes()
    assert manifest["artifact_sha256"] == {name: digest(OUT / name) for name in ARTIFACTS}
    assert manifest["commands"]["phase9a_runner"] == 0
    runner_exit = int((OUT / "phase9a_runner.exit").read_text(encoding="utf-8-sig").strip())
    assert runner_exit == 0
    counts, evidence = {}, {}
    for name in VALIDATIONS:
        exit_path = OUT / f"phase9a_{name}.exit"
        log_path = OUT / f"phase9a_{name}.log"
        code = int(exit_path.read_text(encoding="utf-8-sig").strip())
        assert code == 0, (name, code)
        content = log_path.read_text(encoding="utf-8-sig")
        if name in ("repository_tests", "v2_tests", "backend_tests"):
            matches = re.findall(r"Ran (\d+) tests? in [\d.]+s", content)
            assert len(matches) == 1 and re.search(r"(?m)^OK\s*$", content), name
            assert not re.search(r"(?mi)^(?:FAILED|OK \(|.*\.\.\. skipped)", content), name
            counts[name] = int(matches[0])
            if name == "repository_tests":
                assert re.search(r"(?m)^test_engine_and_offline_ui .*\.\.\. ok$", content)
                assert "IndexedDB and CSV/JSON workspace passed" in content
                counts["node_embedded_regressions"] = 2
        elif name == "frontend_tests":
            counts[name] = int(re.findall(r"Tests\s+(\d+) passed", content)[-1])
        manifest["commands"][name] = code
        evidence[log_path.name] = digest(log_path)
        evidence[exit_path.name] = digest(exit_path)
    network = OUT / "phase9a_network_denials.log"
    assert not network.exists() or not network.read_text(encoding="utf-8-sig").strip()
    initial_path = OUT / "phase9a_protection_before.json"
    assert manifest["protection_initial_sha256"] == digest(initial_path)
    assert json.loads(initial_path.read_bytes())["sha256"] == before
    evidence["phase9a_runner.exit"] = digest(OUT / "phase9a_runner.exit")
    evidence["phase9a_runner.log"] = digest(OUT / "phase9a_runner.log")
    assert guard.attempts == manifest["market_http_requests"] == 0
    manifest.update(state="verified_offline", validated_at_utc=now(), test_counts=counts, verification_sha256=evidence,
                    protected_after=protected(ROOT))
    assert before == manifest["protected_after"]
    write_json(MANIFEST, manifest)
    print(f"验证完成，{PHASE8_FILES} 个文件和 Phase 8 清单 SHA-256 前后一致，{counts}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finalize-validation", action="store_true")
    args = parser.parse_args()
    require_runtime_guard()
    guard = Offline()
    sys.addaudithook(guard)
    if args.finalize_validation:
        finalize(guard)
    else:
        run(guard)


if __name__ == "__main__":
    main()
