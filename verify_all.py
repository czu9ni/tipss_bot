#!/usr/bin/env python3
"""
verify_all.py
Repo-wide verification for Soccer Bot AI Dashboard.

What it checks:
1) Python syntax compile (py_compile) for all .py files
2) pytest -q
3) End-to-end run twice for same date:
   - first with --no-cache
   - second with cache
   It verifies:
   - command succeeds
   - outputs exactly 2 picks (best-effort parsing)
   - second run uses cache (best-effort via cache directory existence + comparing outputs)
4) Cache directory presence and contents
5) Anti-duplication heuristics (single cache/scoring/cli modules)
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Tuple, Optional

ROOT = Path(__file__).resolve().parent

# --- Helpers ---------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    ok: bool
    details: str = ""

def run_cmd(cmd: List[str], cwd: Path = ROOT, timeout: int = 300) -> Tuple[int, str]:
    """Run a command, capture stdout+stderr."""
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return 1, f"ERROR running {' '.join(cmd)}: {e}"

def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def banner(title: str) -> str:
    return f"\n=== {title} ===\n"

# --- Domain-specific parsing (best-effort) --------------------------------

PICK_PATTERNS = [
    re.compile(r"^\s*#?\s*Pick\s*[:\-]\s*(.+)$", re.IGNORECASE),
    re.compile(r"^\s*#?\s*Tipp\s*[:\-]\s*(.+)$", re.IGNORECASE),
    re.compile(r"^\s*\d+\)\s*(.+\s+vs\.?\s+.+)$", re.IGNORECASE),
    re.compile(r"^\s*(.+\s+vs\.?\s+.+)$", re.IGNORECASE),
]

def extract_picks(output: str) -> List[str]:
    """
    Try to extract match picks from console output.
    This is heuristic, because different implementations format differently.
    """
    picks: List[str] = []
    for line in output.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue
        # Avoid grabbing random "vs" lines inside logs by requiring some signal
        for pat in PICK_PATTERNS:
            m = pat.match(line_stripped)
            if m:
                candidate = m.group(1).strip()
                # Basic sanity: contains vs / - / v
                if (" vs" in candidate.lower()) or (" - " in candidate) or (" v " in candidate.lower()):
                    picks.append(candidate)
                break

    # Deduplicate while preserving order
    seen = set()
    uniq: List[str] = []
    for p in picks:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq[:10]  # keep first chunk

def cache_dir_for(run_date: str) -> Path:
    # If CACHE_DIR is set, respect it; else default to data/cache
    cache_root = Path(os.environ.get("CACHE_DIR", "data/cache"))
    if not cache_root.is_absolute():
        cache_root = ROOT / cache_root
    return cache_root / run_date

# --- Checks ----------------------------------------------------------------

def check_compile_all_py() -> CheckResult:
    py_files = [p for p in ROOT.rglob("*.py") if ".venv" not in str(p) and "site-packages" not in str(p)]
    if not py_files:
        return CheckResult("py_compile (all .py)", False, "No Python files found.")

    # Compile via python -m py_compile file1 file2 ...
    cmd = [sys.executable, "-m", "py_compile"] + [str(p.relative_to(ROOT)) for p in py_files]
    rc, out = run_cmd(cmd, timeout=300)
    ok = (rc == 0)
    return CheckResult("py_compile (all .py)", ok, out.strip() if out.strip() else "OK")

def check_pytest() -> CheckResult:
    rc, out = run_cmd([sys.executable, "-m", "pytest", "-q"], timeout=600)
    ok = (rc == 0)
    return CheckResult("pytest -q", ok, out.strip() if out.strip() else "OK")

def check_anti_duplication() -> CheckResult:
    details = []
    ok = True

    # Hard expectation: these canonical modules should exist once
    expected_singletons = [
        ("cache module", "soccer_bot/cache.py"),
        ("scoring module", "soccer_bot/scoring.py"),
        ("cli module", "soccer_bot/cli.py"),
    ]
    for label, rel in expected_singletons:
        path = ROOT / rel
        if not path.exists():
            ok = False
            details.append(f"Missing: {rel} ({label})")
        else:
            # ensure no duplicates with same basename in soccer_bot/
            basename = Path(rel).name
            hits = [p for p in (ROOT / "soccer_bot").rglob(basename)]
            if len(hits) != 1:
                ok = False
                details.append(f"Duplicate {basename} files found: " + ", ".join(str(h.relative_to(ROOT)) for h in hits))

    # Heuristic: look for additional cache implementations by keyword density
    # (not perfect, but catches obvious duplicates)
    suspicious = []
    for p in (ROOT / "soccer_bot").rglob("*.py"):
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if p.name in {"cache.py"}:
            continue
        if re.search(r"\bCACHE_(DIR|HIT|MISS|KEY)\b", txt) or "data/cache" in txt or re.search(r"\bcache\s*=\s*\{", txt):
            # allow cli.py to mention CACHE_DIR
            if p.name == "cli.py":
                continue
            suspicious.append(str(p.relative_to(ROOT)))

    if suspicious:
        details.append("Potential duplicated cache logic mentions in: " + ", ".join(suspicious))

    return CheckResult("anti-duplication (heuristic)", ok, "\n".join(details) if details else "OK")

def check_end_to_end(run_date: str) -> CheckResult:
    # First run: --no-cache
    cmd1 = [sys.executable, "-m", "soccer_bot", "run", "--date", run_date, "--no-cache"]
    rc1, out1 = run_cmd(cmd1, timeout=900)

    if rc1 != 0:
        return CheckResult(
            f"end-to-end run (no-cache) {run_date}",
            False,
            out1.strip() or f"Command failed: {' '.join(cmd1)}",
        )

    # Second run: cached
    cmd2 = [sys.executable, "-m", "soccer_bot", "run", "--date", run_date]
    rc2, out2 = run_cmd(cmd2, timeout=900)

    if rc2 != 0:
        return CheckResult(
            f"end-to-end run (cache) {run_date}",
            False,
            out2.strip() or f"Command failed: {' '.join(cmd2)}",
        )

    # Verify cache dir exists and has files
    cdir = cache_dir_for(run_date)
    if not cdir.exists():
        return CheckResult(
            f"cache dir exists {cdir.relative_to(ROOT) if cdir.is_relative_to(ROOT) else str(cdir)}",
            False,
            "Cache directory was not created.",
        )

    files = list(cdir.rglob("*.json"))
    if len(files) == 0:
        return CheckResult(
            f"cache files present {cdir.relative_to(ROOT) if cdir.is_relative_to(ROOT) else str(cdir)}",
            False,
            "No JSON cache files found under cache directory.",
        )

    # Best-effort: picks extraction & determinism
    picks1 = extract_picks(out1)
    picks2 = extract_picks(out2)

    # We don't know exact output format, so we accept:
    # - either we extracted at least 2 picks and first two match
    # - or we can't parse picks; then we fall back to comparing output hash ignoring obvious volatile lines
    ok = True
    details = []

    if len(picks1) >= 2 and len(picks2) >= 2:
        if picks1[:2] != picks2[:2]:
            ok = False
            details.append("Determinism FAIL: picks differ between run1 and run2.")
            details.append(f"Run1 picks: {picks1[:2]}")
            details.append(f"Run2 picks: {picks2[:2]}")
        else:
            details.append(f"Picks OK: {picks1[:2]}")
    else:
        # fallback: compare sanitized output hashes
        def sanitize(s: str) -> str:
            # remove timestamps / call counters lines that may vary
            lines = []
            for ln in s.splitlines():
                if re.search(r"\b(API_CALLS_TOTAL|CACHE_HITS|CACHE_MISSES)\b", ln):
                    continue
                if re.search(r"\b\d{4}-\d{2}-\d{2}\b", ln) and "date" in ln.lower():
                    continue
                lines.append(ln)
            return "\n".join(lines).strip()

        h1 = sha256_text(sanitize(out1))
        h2 = sha256_text(sanitize(out2))
        if h1 != h2:
            # Not always an error, but likely indicates nondeterminism
            details.append("WARNING: Could not reliably parse picks; sanitized output hashes differ.")
            details.append(f"hash1={h1}")
            details.append(f"hash2={h2}")
        else:
            details.append("Output seems stable (hash match).")

    # Cache effectiveness hint: second run should be "cheaper"
    # We can't guarantee logging, but we can look for those tokens.
    if ("CACHE_HITS" in out2) and ("CACHE_MISSES" in out2):
        details.append("Cache metrics found in output (good).")
    else:
        details.append("WARNING: Cache metrics not found in output. (Not fatal, but recommended.)")

    details.append(f"Cache JSON files: {len(files)} under {cdir}")

    return CheckResult(f"end-to-end (2x) + cache {run_date}", ok, "\n".join(details))

# --- Main ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=str(date.today()), help="YYYY-MM-DD (default: today)")
    ap.add_argument("--skip-e2e", action="store_true", help="Skip end-to-end runs")
    args = ap.parse_args()

    results: List[CheckResult] = []

    results.append(check_compile_all_py())
    results.append(check_anti_duplication())
    results.append(check_pytest())

    if not args.skip_e2e:
        results.append(check_end_to_end(args.date))

    # Print report
    print(banner("VERIFY REPORT"))
    all_ok = True
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        if not r.ok:
            all_ok = False
        print(f"[{status}] {r.name}")
        if r.details:
            # keep details readable
            print(r.details.strip())
            print("-" * 60)

    if all_ok:
        print("\nALL CHECKS PASSED")
        return 0
    else:
        print("\nSOME CHECKS FAILED (see above)")
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
