#!/usr/bin/env python3
"""Fail CI when locked PyPI packages have known OSV advisories."""

import json
import re
import sys
import urllib.request
from pathlib import Path

OSV_QUERY_BATCH = "https://api.osv.dev/v1/querybatch"
LOCK_ENTRY = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", re.MULTILINE)


def parse_lock(path: str) -> list[tuple[str, str]]:
    content = Path(path).read_text(encoding="utf-8")
    packages = LOCK_ENTRY.findall(content)
    if not packages:
        raise ValueError("Lock file contains no pinned packages")
    return packages


def query_osv(packages: list[tuple[str, str]]) -> list[dict]:
    payload = {
        "queries": [
            {"package": {"ecosystem": "PyPI", "name": name}, "version": version}
            for name, version in packages
        ]
    }
    request = urllib.request.Request(
        OSV_QUERY_BATCH,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        results = json.load(response).get("results", [])
    if len(results) != len(packages):
        raise RuntimeError("OSV returned an incomplete response")
    return results


def main(path: str = "requirements.lock") -> int:
    packages = parse_lock(path)
    findings = []
    for (name, version), result in zip(packages, query_osv(packages)):
        for vulnerability in result.get("vulns", []):
            findings.append(f"{name}=={version}: {vulnerability['id']}")
    if findings:
        print("Known dependency vulnerabilities found:")
        print("\n".join(findings))
        return 1
    print(f"OSV: no known vulnerabilities in {len(packages)} locked packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "requirements.lock"))
