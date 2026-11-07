#!/usr/bin/env python3
"""Consolidate Dependabot pull requests into a human-authored branch.

Why this exists: in this repository Dependabot PRs never receive CI runs
(their head commits carry zero check-runs), so branch protection — which
requires the `test` and `dependency-audit` checks with
`enforce_admins: true` — can never be satisfied. This tool takes the
Dependabot diff, replays it on a branch pushed by the repository owner
(which does trigger CI), verifies it locally, and only then merges.

Gates, in order. A failure at any gate aborts that PR and reports why:
  1. the Dependabot patch applies cleanly onto current master
  2. every pip bump gets a refreshed requirements.lock entry (version +
     hash) and the whole lock validates with --require-hashes for the
     CI target (CPython 3.11 / manylinux x86_64)
  3. the unit suite passes locally on CPython 3.11
  4. the CI checks on the pushed PR go green

Usage:
    python tools/dependabot_consolidate.py            # act
    python tools/dependabot_consolidate.py --dry-run  # verify only
    python tools/dependabot_consolidate.py --pr 37    # single PR
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "emowbaik/auto-vote-topgg"
BASE = "master"
PY_TARGET = ("3.11", "cp311", "manylinux2014_x86_64")
LOCK = "requirements.lock"
REQ = "requirements.txt"
LOCK_HEADER = "# Linux x86_64 / CPython 3.11 lock; regenerate per README.\r\n"
CHECK_TIMEOUT_SEC = 900
CHECK_POLL_SEC = 20


def log(message: str = "") -> None:
    print(message, flush=True)


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=capture,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"{(result.stderr or result.stdout or '').strip()[:2000]}"
        )
    return result


def gh(*args: str, check: bool = True) -> str:
    return run(["gh", *args], check=check).stdout


def open_dependabot_prs() -> list[dict]:
    raw = gh(
        "pr", "list", "--repo", REPO, "--state", "open", "--limit", "50",
        "--json", "number,title,files,headRefName,author",
    )
    prs = json.loads(raw)
    return [
        pr for pr in prs
        if pr.get("author", {}).get("login", "").startswith("app/dependabot")
    ]


def pypi_hashes(package: str, version: str) -> list[str]:
    """sha256 of the artefacts the CI runner would install for this version."""
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{package}=={version}: PyPI returned {exc.code} (bad version?)"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{package}=={version}: PyPI unreachable ({exc.reason})") from exc
    py_version, abi, platform = PY_TARGET
    cp = "cp" + py_version.replace(".", "")

    def score(filename: str) -> int:
        if filename.endswith(".whl"):
            if f"-{cp}-{abi}-" in filename and platform in filename:
                return 0
            if f"-{cp}-{abi}-" in filename:
                return 1
            if "py3-none-any" in filename:
                return 2
            if "abi3" in filename and platform in filename:
                return 3
            return 9
        return 8

    candidates = sorted(data["urls"], key=lambda item: score(item["filename"]))
    if not candidates:
        raise RuntimeError(f"{package}=={version} has no artefacts on PyPI")
    best = candidates[0]
    if score(best["filename"]) == 9:
        raise RuntimeError(
            f"{package}=={version}: no wheel usable by the CI target "
            f"({best['filename']})"
        )
    return [best["digests"]["sha256"]]


def parse_lock(text: str) -> dict[str, tuple[str, list[str]]]:
    entries: dict[str, tuple[str, list[str]]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("--hash="):
            if current is None:
                continue
            value = stripped.split("=", 1)[1]
            if ":" in value:
                value = value.split(":", 1)[1]
            entries[current][1].append(value)
            continue
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", stripped)
        if match:
            current = match.group(1).lower()
            entries[current] = (match.group(2), [])
    return entries


def render_lock(entries: dict[str, tuple[str, list[str]]], newline: str = "\n",
                header: str = "") -> str:
    lines: list[str] = []
    if header:
        lines.append(header)
    for name in sorted(entries):
        version, hashes = entries[name]
        lines.append(f"{name}=={version} \\")
        for index, digest in enumerate(hashes):
            suffix = " \\" if index < len(hashes) - 1 else ""
            lines.append(f"    --hash=sha256:{digest}{suffix}")
    return newline.join(lines) + newline


def parse_requirements(text: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;#]+)", line.strip())
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def refresh_lock(workdir: Path) -> list[str]:
    """Align lock pins with requirements.txt pin changes; return change notes."""
    lock_path = workdir / LOCK
    raw = lock_path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8")
    header = ""
    for line in text.splitlines():
        if line.strip().startswith("#"):
            header = line
            break

    req_pins = parse_requirements((workdir / REQ).read_text(encoding="utf-8"))
    lock_entries = parse_lock(text)
    base_pins = parse_requirements(
        run(["git", "show", f"origin/{BASE}:{REQ}"], cwd=workdir).stdout
    )

    notes: list[str] = []
    for name, version in req_pins.items():
        if base_pins.get(name) == version:
            continue
        if name not in lock_entries:
            raise RuntimeError(
                f"{name}=={version} is not tracked in {LOCK}; needs manual lock work"
            )
        current_version, _ = lock_entries[name]
        if current_version == version:
            continue
        hashes = pypi_hashes(name, version)
        lock_entries[name] = (version, hashes)
        notes.append(f"{name}: {current_version} -> {version} (hash refreshed)")

    rendered = render_lock(lock_entries, newline=newline, header=header).encode("utf-8")
    if rendered != raw:
        lock_path.write_bytes(rendered)
    return notes


def pip_python() -> str:
    """First interpreter on PATH that can actually run pip."""
    candidates = ["python3.11", "python3.12", "python3", sys.executable]
    for candidate in candidates:
        path = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if not path:
            continue
        probe = subprocess.run([path, "-m", "pip", "--version"],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            return path
    raise RuntimeError("no interpreter with pip available")


def venv_base_python() -> str:
    """Interpreter used to build the local test venv (must match CI: 3.11)."""
    for candidate in ("python3.11", "python3.12"):
        path = shutil.which(candidate)
        if path:
            return path
    return pip_python()


def validate_lock(workdir: Path) -> None:
    py_version, abi, platform = PY_TARGET
    with tempfile.TemporaryDirectory() as target:
        run(
            [
                pip_python(), "-m", "pip", "install", "--dry-run", "--quiet",
                "--ignore-installed", "--only-binary=:all:",
                "--platform", platform,
                "--python-version", py_version,
                "--implementation", "cp", "--abi", abi,
                "--target", target, "--require-hashes",
                "-r", str(workdir / LOCK),
            ],
            cwd=workdir,
        )


def chrome_binary() -> str | None:
    """A Chrome the unit suite can construct uc.Config() with."""
    override = os.environ.get("CHROME_BIN")
    if override and Path(override).exists():
        return override
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    cache = Path.home() / ".cache" / "ms-playwright"
    candidates = sorted(cache.glob("chromium-*/chrome-linux*/chrome"), reverse=True)
    return str(candidates[0]) if candidates else None


def run_unit_tests(workdir: Path, venv: Path) -> None:
    if not (venv / "bin" / "python").exists():
        run([venv_base_python(), "-m", "venv", str(venv)], cwd=workdir)
        run([str(venv / "bin" / "python"), "-m", "pip", "install", "-q", "-U", "pip"])
    stripped = workdir / ".dependabot-lock-nohash.txt"
    stripped.write_text(
        "\n".join(
            line for line in (workdir / LOCK).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
            and not line.strip().startswith("--hash=")
        ).replace(" \\", ""),
        encoding="utf-8",
    )
    run([str(venv / "bin" / "python"), "-m", "pip", "install", "-q",
         "-r", str(stripped)], cwd=workdir)
    env = dict(os.environ)
    chrome = chrome_binary()
    if chrome:
        env["CHROME_BIN"] = chrome
    result = run(
        [str(venv / "bin" / "python"), "-m", "unittest"],
        cwd=workdir, check=False, env=env,
    )
    output = (result.stderr or "") + (result.stdout or "")
    stripped.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError("unit tests failed:\n" + output.strip()[-2000:])
    match = re.search(r"Ran (\d+) tests", output)
    log(f"  unit tests: {match.group(1) if match else '?'} passed")
    run([str(venv / "bin" / "python"), "-m", "py_compile", "vote.py",
         "test_vote.py", "audit_dependencies.py"], cwd=workdir)


def apply_pr(workdir: Path, pr: dict) -> None:
    patch = gh("pr", "diff", str(pr["number"]), "--repo", REPO)
    patch_file = workdir / f".dependabot-{pr['number']}.patch"
    patch_file.write_text(patch, encoding="utf-8")
    result = run(
        ["git", "apply", "--3way", "--whitespace=nowarn", str(patch_file)],
        cwd=workdir, check=False,
    )
    patch_file.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"patch did not apply: {(result.stderr or result.stdout).strip()[:800]}"
        )


def wait_for_checks(pr_number: int) -> bool:
    deadline = time.time() + CHECK_TIMEOUT_SEC
    while time.time() < deadline:
        raw = gh("pr", "view", str(pr_number), "--repo", REPO,
                 "--json", "mergeStateStatus,statusCheckRollup", check=False)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            time.sleep(CHECK_POLL_SEC)
            continue
        checks = data.get("statusCheckRollup") or []
        states = {
            (check.get("name") or check.get("context", "?")):
                (check.get("conclusion") or check.get("state") or check.get("status"))
            for check in checks
        }
        pending = [n for n, s in states.items() if s in (None, "", "IN_PROGRESS", "QUEUED", "PENDING")]
        failing = [n for n, s in states.items() if s in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR")]
        log(f"  checks: {states or 'none yet'}")
        if failing:
            return False
        if checks and not pending and data.get("mergeStateStatus") == "CLEAN":
            return True
        time.sleep(CHECK_POLL_SEC)
    log("  checks: timed out")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="verify everything but do not push or merge")
    parser.add_argument("--pr", type=int, action="append", default=None,
                        help="limit to specific PR number(s)")
    parser.add_argument("--repo-dir", default=".",
                        help="path to the local clone (default: cwd)")
    args = parser.parse_args()

    workdir = Path(args.repo_dir).expanduser().resolve()
    if not (workdir / ".git").exists():
        log(f"not a git repository: {workdir}")
        return 2

    log(f"repo: {workdir}")
    run(["git", "fetch", "--quiet", "origin", "--prune"], cwd=workdir)

    if args.pr:
        prs = []
        for number in args.pr:
            raw = gh("pr", "view", str(number), "--repo", REPO,
                     "--json", "number,title,files,headRefName,author", check=False)
            try:
                prs.append(json.loads(raw))
            except json.JSONDecodeError:
                log(f"#{number}: not found")
        if not prs:
            log("no usable PRs — nothing to do")
            return 0
    else:
        prs = open_dependabot_prs()
    if not prs:
        log("no open Dependabot PRs — nothing to do")
        return 0

    for pr in prs:
        log(f"\n#{pr['number']} {pr['title']}")

    branch = f"chore/dependabot-{time.strftime('%Y%m%d-%H%M')}"
    run(["git", "checkout", "--quiet", "-B", branch, f"origin/{BASE}"], cwd=workdir)

    applied: list[dict] = []
    failed: list[tuple[int, str]] = []
    for pr in prs:
        log(f"  applying #{pr['number']} ...")
        try:
            apply_pr(workdir, pr)
            applied.append(pr)
        except RuntimeError as exc:
            failed.append((pr["number"], str(exc)))
            log(f"  SKIP #{pr['number']}: {exc}")

    if not applied:
        log("\nnothing applicable — aborting")
        for number, reason in failed:
            log(f"  #{number}: {reason}")
        run(["git", "checkout", "--quiet", BASE], cwd=workdir, check=False)
        return 1

    notes: list[str] = []
    try:
        log("  refreshing lock ...")
        notes = refresh_lock(workdir)
        for note in notes:
            log(f"    {note}")
        if not notes:
            log("    lock already in sync")
        log("  validating lock for CPython 3.11 / manylinux x86_64 ...")
        validate_lock(workdir)
        log("  lock OK")
        log("  running unit suite ...")
        run_unit_tests(workdir, Path("/tmp/dependabot-consolidate-venv"))
    except RuntimeError as exc:
        log(f"\nVERIFICATION FAILED: {exc}")
        run(["git", "checkout", "--quiet", "--", "."], cwd=workdir, check=False)
        run(["git", "checkout", "--quiet", BASE], cwd=workdir, check=False)
        run(["git", "branch", "-D", branch], cwd=workdir, check=False)
        return 1

    if args.dry_run:
        log("\ndiff to be committed:")
        log(run(["git", "diff", "--stat"], cwd=workdir).stdout.strip())
        run(["git", "checkout", "--quiet", "--", "."], cwd=workdir, check=False)
        run(["git", "checkout", "--quiet", BASE], cwd=workdir, check=False)
        run(["git", "branch", "-D", branch], cwd=workdir, check=False)
        log("dry run: nothing pushed")
        return 0

    numbers = ", ".join(f"#{pr['number']}" for pr in applied)
    body_lines = [
        f"Consolidates {numbers} into a human-authored branch.",
        "",
        "Dependabot PRs in this repository never receive CI runs (their head",
        "commits carry zero check-runs), so required checks can never pass.",
        "Replaying the same diffs on an owner-pushed branch triggers CI.",
        "",
    ]
    if notes:
        body_lines.append("Lock refresh:")
        body_lines.extend(f"- {note}" for note in notes)
        body_lines.append("")
    body_lines.append("Gates: patch applies -> lock validates with "
                      "--require-hashes for CPython 3.11/x86_64 -> unit suite "
                      "passes -> CI green.")
    body = "\n".join(body_lines)

    run(["git", "add", "-A"], cwd=workdir)
    run([
        "git", "-c", "user.name=emowbaik",
        "-c", "user.email=emowbaik@users.noreply.github.com",
        "commit", "--quiet", "-m",
        f"chore(deps): consolidate dependabot updates ({numbers})",
    ], cwd=workdir)
    run(["git", "push", "--quiet", "-u", "origin", branch], cwd=workdir)
    log(f"\npushed {branch}")

    pr_url = gh(
        "pr", "create", "--repo", REPO, "--base", BASE, "--head", branch,
        "--title", f"chore(deps): consolidate dependabot updates ({numbers})",
        "--body", body,
    ).strip()
    new_pr = int(pr_url.rstrip("/").split("/")[-1])
    log(f"opened {pr_url}")

    if not wait_for_checks(new_pr):
        log("\nCI not green — PR left open for inspection, Dependabot PRs untouched")
        return 1

    log("  CI green, merging ...")
    gh("pr", "merge", str(new_pr), "--repo", REPO, "--squash", "--delete-branch")
    head = gh("api", f"repos/{REPO}/commits/{BASE}", "--jq", ".sha[0:7]").strip()
    log(f"  merged as {head}")

    for pr in applied:
        gh("pr", "close", str(pr["number"]), "--repo", REPO, "--comment",
           f"Superseded by #{new_pr} (merged to {BASE} as {head}). See that PR "
           f"for the verification gates.", check=False)
        log(f"  closed #{pr['number']}")

    if failed:
        log("\nstill needs a human:")
        for number, reason in failed:
            log(f"  #{number}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
