#!/usr/bin/env python3
"""Validate a proxy before using it for top.gg automation.

Checks, in order:
  1. TCP reachability of the proxy endpoint
  2. HTTP CONNECT works through the proxy (real fetch, not just TCP open)
  3. the exit IP's class (datacenter vs residential) via ip-api
  4. whether top.gg still serves a Cloudflare challenge through this exit

Exit codes:
  0  proxy works and top.gg is reachable without a challenge
  1  proxy reaches top.gg but is still challenged
  2  proxy is not usable (TCP closed, fetch failed, or bad configuration)
"""

import json
import os
import re
import socket
import subprocess
import sys
from urllib.parse import urlparse

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
ECHO = "http://ip-api.com/json/?fields=status,countryCode,as,hosting,proxy,query"
VOTE = "https://top.gg/bot/830530156048285716/vote"


def describe(proxy: str) -> str:
    text = (proxy or "").strip()
    if not text:
        return "none"
    parsed = urlparse(text if "://" in text else f"http://{text}")
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}{port}"


def tcp_open(bare: str, timeout: float = 6.0) -> bool:
    host, _, port = bare.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def curl(proxy: str, url: str, timeout: int) -> tuple[str, str]:
    cmd = ["curl", "-s", "-m", str(timeout), "-x", proxy, "-A", UA, "-w", "\n__CODE:%{http_code}", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 6).stdout
    except Exception:
        return "000", ""
    m = re.search(r"__CODE:(\d+)\s*$", out)
    return (m.group(1) if m else "000"), (out[: m.start()] if m else out)


def is_challenge(text: str) -> bool:
    low = text.lower()
    return any(
        marker in low
        for marker in (
            "just a moment",
            "performing security verification",
            "verify you are not a bot",
            "challenge-platform",
            "cf_chl",
            "enable javascript and cookies to continue",
            "attention required",
        )
    )


def main() -> int:
    proxy = (os.environ.get("TOPGG_PROXY") or "").strip()
    if not proxy:
        print("TOPGG_PROXY is not set; direct connection would be used.")
        print("RESULT: no-proxy")
        return 2
    bare = proxy.split("://", 1)[-1]
    if "@" in bare:
        bare = bare.rsplit("@", 1)[-1]
    print(f"Proxy: {describe(proxy)}")

    if not tcp_open(bare):
        print("  ✗ TCP connect failed")
        print("RESULT: unusable")
        return 2

    code, body = curl(proxy, ECHO, 20)
    if code != "200":
        print(f"  ✗ ip-api echo returned HTTP {code}")
        print("RESULT: unusable")
        return 2
    try:
        info = json.loads(body)
    except Exception:
        print("  ✗ ip-api returned non-JSON")
        print("RESULT: unusable")
        return 2
    if info.get("status") != "success":
        print("  ✗ ip-api status:", info.get("status"))
        print("RESULT: unusable")
        return 2

    hosting = info.get("hosting")
    print(f"  ✓ exit IP {info.get('query')} ({info.get('countryCode')}) "
          f"hosting={hosting} asn={info.get('as')}")

    code, body = curl(proxy, VOTE, 30)
    if code == "000":
        print("  ✗ top.gg fetch failed through the proxy")
        print("RESULT: unusable")
        return 2
    if code == "200" and not is_challenge(body):
        print(f"  ✓ top.gg reachable (HTTP {code}) without a challenge")
        print("RESULT: usable")
        return 0
    if is_challenge(body):
        print(f"  ✗ top.gg still serves a Cloudflare challenge (HTTP {code})")
        print("RESULT: challenged")
        return 1
    print(f"  ! top.gg returned HTTP {code} (not a challenge, not 200)")
    print("RESULT: unexpected")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
