"""Read-only quota and token-activity collectors for local coding-agent logins."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

PROTOCOL = "claude-control.provider-usage.v1"
PROVIDERS = ("codex", "claude", "gemini", "cursor")
MAX_RESPONSE_BYTES = 1024 * 1024
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _clamp(value):
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _epoch(value):
    if isinstance(value, dict):
        value = value.get("seconds") or value.get("value") or value.get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _window(
    label,
    *,
    used_percent=None,
    remaining_percent=None,
    reset_at=None,
    unit="percent",
    used=None,
    remaining=None,
    limit=None,
    model=None,
    note=None,
):
    used_percent = _clamp(used_percent)
    remaining_percent = _clamp(remaining_percent)
    if used_percent is None and remaining_percent is not None:
        used_percent = 100.0 - remaining_percent
    if remaining_percent is None and used_percent is not None:
        remaining_percent = 100.0 - used_percent
    result = {
        "label": label,
        "used_percent": used_percent,
        "remaining_percent": remaining_percent,
        "resets_at": _epoch(reset_at),
        "unit": unit,
        "used": used,
        "remaining": remaining,
        "limit": limit,
        "exact_amounts": used is not None and remaining is not None and limit is not None,
    }
    if model:
        result["model"] = str(model)
    if note:
        result["note"] = str(note)
    return result


def _base(
    provider,
    *,
    installed,
    authenticated,
    status,
    source,
    windows=None,
    tokens=None,
    spending=None,
    note=None,
    error=None,
):
    value = {
        "provider": provider,
        "installed": bool(installed),
        "authenticated": bool(authenticated),
        "status": status,
        "source": source,
        "fetched_at": time.time(),
        "stale": False,
        "windows": windows or [],
        "tokens": tokens or {},
        "spending": spending or {},
    }
    if note:
        value["note"] = str(note)
    if error:
        value["error"] = _safe_error(error)
    return value


def _safe_error(error):
    text = " ".join(str(error).split())
    home = str(Path.home())
    if home:
        text = text.replace(home, "~")
    return text[:240]


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


class _CodexRpc:
    def __init__(self, binary, timeout):
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            [binary, "app-server", "--stdio"],
            cwd=tempfile.gettempdir(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                self.messages.put(value)

    def send(self, value):
        if self.process.poll() is not None or self.process.stdin is None:
            raise RuntimeError("Codex app server stopped")
        self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def request(self, identifier, method, params):
        self.send({"id": identifier, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex usage request timed out")
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Codex usage request timed out") from exc
            if message.get("id") != identifier:
                continue
            if isinstance(message.get("error"), dict):
                raise RuntimeError(message["error"].get("message") or "Codex request failed")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Codex returned no usage result")
            return result

    def close(self):
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)


def _codex(timeout):
    binary = shutil.which("codex")
    if not binary:
        return _base(
            "codex",
            installed=False,
            authenticated=False,
            status="unavailable",
            source="codex app-server",
        )
    client = None
    try:
        client = _CodexRpc(binary, timeout)
        client.request(
            1,
            "initialize",
            {
                "clientInfo": {
                    "name": "claude-control-monitor",
                    "title": "Claude Control Monitor",
                    "version": "1",
                },
                "capabilities": {},
            },
        )
        client.send({"method": "initialized", "params": {}})
        limits = client.request(2, "account/rateLimits/read", None)
        usage = client.request(3, "account/usage/read", {})
    except Exception as exc:
        return _base(
            "codex",
            installed=True,
            authenticated=False,
            status="error",
            source="codex app-server",
            error=exc,
        )
    finally:
        if client is not None:
            client.close()

    snapshots = limits.get("rateLimitsByLimitId")
    if not isinstance(snapshots, dict) or not snapshots:
        snapshots = {"codex": limits.get("rateLimits")}
    windows = []
    for limit_id, value in snapshots.items():
        if not isinstance(value, dict):
            continue
        name = value.get("limitName") or limit_id or "Codex"
        for slot in ("primary", "secondary"):
            item = value.get(slot)
            if not isinstance(item, dict):
                continue
            minutes = item.get("windowDurationMins")
            period = _period(minutes)
            windows.append(
                _window(
                    f"{name} · {period}",
                    used_percent=item.get("usedPercent"),
                    reset_at=item.get("resetsAt"),
                    note="service-reported quota; absolute token allowance is not published",
                )
            )
    summary = usage.get("summary") if isinstance(usage.get("summary"), dict) else {}
    daily = usage.get("dailyUsageBuckets")
    daily = daily if isinstance(daily, list) else []
    today = daily[-1].get("tokens") if daily and isinstance(daily[-1], dict) else None
    tokens = {
        "unit": "tokens",
        "lifetime_used": summary.get("lifetimeTokens"),
        "today_used": today,
        "peak_daily_used": summary.get("peakDailyTokens"),
        "remaining": None,
        "note": "account token activity; subscription token ceiling is not published",
    }
    authenticated = bool(
        windows
        or summary.get("lifetimeTokens") is not None
        or summary.get("peakDailyTokens") is not None
        or today is not None
    )
    return _base(
        "codex",
        installed=True,
        authenticated=authenticated,
        status="ok" if authenticated else "signed_out",
        source="codex app-server",
        windows=windows,
        tokens=tokens,
    )


def _period(minutes):
    value = _number(minutes)
    if value is None or value <= 0:
        return "window"
    value = int(value)
    if value % 10080 == 0:
        return f"{value // 10080}w"
    if value % 1440 == 0:
        return f"{value // 1440}d"
    if value % 60 == 0:
        return f"{value // 60}h"
    return f"{value}m"


def _claude(_timeout):
    binary = shutil.which("claude")
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
    cache_path = config_dir.parent / (config_dir.name + ".json")
    cache = _read_json(cache_path) or {}
    authenticated = bool(_read_json(config_dir / ".credentials.json") or cache.get("oauthAccount"))
    cached = cache.get("cachedUsageUtilization")
    cached = cached if isinstance(cached, dict) else {}
    utilization = cached.get("utilization")
    utilization = utilization if isinstance(utilization, dict) else {}
    windows = []
    raw_limits = utilization.get("limits")
    if isinstance(raw_limits, list) and raw_limits:
        for item in raw_limits:
            if not isinstance(item, dict):
                continue
            label = str(item.get("kind") or item.get("group") or "Claude").replace("_", " ")
            scope = item.get("scope")
            model = None
            if isinstance(scope, dict) and isinstance(scope.get("model"), dict):
                model = scope["model"].get("display_name") or scope["model"].get("id")
            windows.append(
                _window(
                    label,
                    used_percent=item.get("percent"),
                    reset_at=item.get("resets_at"),
                    model=model,
                    note="service-reported quota; absolute token allowance is not published",
                )
            )
    else:
        for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
            item = utilization.get(key)
            if isinstance(item, dict):
                windows.append(
                    _window(
                        label,
                        used_percent=item.get("utilization"),
                        reset_at=item.get("resets_at"),
                        note="service-reported quota; absolute token allowance is not published",
                    )
                )
    spending = {}
    spend = utilization.get("spend")
    if isinstance(spend, dict):
        amount = spend.get("used")
        if isinstance(amount, dict):
            minor = _number(amount.get("amount_minor"))
            exponent = _number(amount.get("exponent"))
            if minor is not None and exponent is not None:
                spending["used"] = minor / (10 ** int(exponent))
                spending["currency"] = amount.get("currency")
        spending["limit"] = spend.get("limit")
        spending["enabled"] = bool(spend.get("enabled"))
    fetched_ms = _number(cached.get("fetchedAtMs"))
    result = _base(
        "claude",
        installed=bool(binary),
        authenticated=authenticated,
        status="ok" if windows else ("signed_in_no_data" if authenticated else "signed_out"),
        source="Claude Code usage cache",
        windows=windows,
        spending=spending,
        tokens={
            "unit": "tokens",
            "remaining": None,
            "note": "account token ceiling is not published; managed-run totals appear in this monitor",
        },
    )
    if fetched_ms is not None:
        result["fetched_at"] = fetched_ms / 1000
        result["stale"] = time.time() - result["fetched_at"] > 900
    return result


def _post_json(url, token, payload, timeout, extra_headers=None):
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": "claude-control-monitor/1",
    }
    headers.update(extra_headers or {})
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError("saved sign-in has expired") from exc
        raise RuntimeError(f"provider API returned HTTP {exc.code}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("provider usage response exceeded 1 MiB")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("provider returned invalid usage JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("provider returned an invalid usage payload")
    return value


def _gemini(timeout):
    binary = shutil.which("gemini")
    config_dir = Path(os.environ.get("GEMINI_CLI_HOME", Path.home() / ".gemini")).expanduser()
    credentials = _read_json(config_dir / "oauth_creds.json") or {}
    token = credentials.get("access_token")
    if not binary and not token:
        return _base(
            "gemini",
            installed=False,
            authenticated=False,
            status="unavailable",
            source="Gemini Code Assist",
        )
    if not isinstance(token, str) or not token.strip():
        return _base(
            "gemini",
            installed=bool(binary),
            authenticated=False,
            status="signed_out",
            source="Gemini Code Assist",
        )
    expiry = _number(credentials.get("expiry_date"))
    if expiry is not None and expiry / 1000 <= time.time() + 30:
        return _base(
            "gemini",
            installed=bool(binary),
            authenticated=True,
            status="auth_expired",
            source="Gemini Code Assist",
            error="OAuth access token expired; run Gemini CLI to refresh sign-in",
        )
    endpoint = "https://cloudcode-pa.googleapis.com"
    try:
        account = _post_json(
            endpoint + "/v1internal:loadCodeAssist",
            token,
            {"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
            timeout,
        )
        project = account.get("cloudaicompanionProject")
        if isinstance(project, dict):
            project = project.get("id")
        quota = _post_json(
            endpoint + "/v1internal:retrieveUserQuota",
            token,
            {"project": project} if isinstance(project, str) and project else {},
            timeout,
        )
    except Exception as exc:
        return _base(
            "gemini",
            installed=bool(binary),
            authenticated=True,
            status="error",
            source="Gemini Code Assist",
            error=exc,
        )
    windows = []
    for item in quota.get("buckets") or []:
        if not isinstance(item, dict):
            continue
        fraction = _number(item.get("remainingFraction"))
        remaining_percent = None if fraction is None else fraction * 100
        remaining = _number(item.get("remainingAmount"))
        limit = None
        used = None
        if remaining is not None and fraction is not None and fraction > 0:
            limit = round(remaining / fraction)
            used = max(0, limit - remaining)
        token_type = str(item.get("tokenType") or "quota").lower()
        unit = {
            "requests": "requests",
            "input_tokens": "input tokens",
            "output_tokens": "output tokens",
        }.get(token_type, token_type.replace("_", " "))
        windows.append(
            _window(
                str(item.get("modelId") or "Gemini quota"),
                remaining_percent=remaining_percent,
                reset_at=item.get("resetTime"),
                unit=unit,
                used=int(used) if used is not None else None,
                remaining=int(remaining) if remaining is not None else None,
                limit=int(limit) if limit is not None else None,
                model=item.get("modelId"),
                note="Google quota bucket",
            )
        )
    return _base(
        "gemini",
        installed=bool(binary),
        authenticated=True,
        status="ok" if windows else "signed_in_no_data",
        source="Gemini Code Assist",
        windows=windows,
        tokens={
            "unit": "tokens",
            "remaining": None,
            "note": "exact amounts appear only when Google returns token buckets",
        },
    )


def _cursor_auth_candidates(environ=None, home=None):
    """Return bounded local Cursor credential candidates without reading them."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    candidates = []
    explicit = environ.get("CURSOR_AUTH_FILE")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    xdg = environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg) / "cursor" / "auth.json")
    appdata = environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Cursor" / "auth.json")
    localappdata = environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(Path(localappdata) / "Cursor" / "auth.json")
    candidates.extend((home / ".config/cursor/auth.json", home / ".cursor/auth.json"))
    return candidates


def _cursor_auth():
    for path in _cursor_auth_candidates():
        value = _read_json(path)
        token = value.get("accessToken") if value else None
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def _cursor(timeout):
    binary = shutil.which("agent") or shutil.which("cursor") or shutil.which("cursor-agent")
    token = _cursor_auth()
    if not binary and not token:
        return _base(
            "cursor",
            installed=False,
            authenticated=False,
            status="unavailable",
            source="Cursor dashboard",
        )
    if not token:
        return _base(
            "cursor",
            installed=bool(binary),
            authenticated=False,
            status="signed_out",
            source="Cursor dashboard",
        )
    endpoint = os.environ.get("CURSOR_API_ENDPOINT", "https://api2.cursor.sh").rstrip("/")
    path = "/aiserver.v1.DashboardService"
    headers = {"Connect-Protocol-Version": "1"}
    try:
        usage = _post_json(endpoint + path + "/GetCurrentPeriodUsage", token, {}, timeout, headers)
        try:
            plan = _post_json(endpoint + path + "/GetPlanInfo", token, {}, timeout, headers)
        except Exception:
            plan = {}
    except Exception as exc:
        return _base(
            "cursor",
            installed=bool(binary),
            authenticated=True,
            status="error",
            source="Cursor dashboard",
            error=exc,
        )
    plan_usage = usage.get("planUsage")
    windows = []
    if isinstance(plan_usage, dict) and _number(plan_usage.get("totalPercentUsed")) is not None:
        windows.append(
            _window(
                "billing cycle",
                used_percent=plan_usage.get("totalPercentUsed"),
                reset_at=usage.get("billingCycleEnd"),
                note="Cursor-reported included usage; absolute token allowance is not published",
            )
        )
    spend = usage.get("spendLimitUsage")
    spending = {}
    if isinstance(spend, dict):
        for source, target in (
            ("individualUsed", "used_cents"),
            ("individualLimit", "limit_cents"),
            ("individualRemaining", "remaining_cents"),
        ):
            value = _number(spend.get(source))
            if value is not None:
                spending[target] = value
    plan_info = plan.get("planInfo") if isinstance(plan.get("planInfo"), dict) else {}
    if plan_info.get("planName"):
        spending["plan"] = plan_info["planName"]
    return _base(
        "cursor",
        installed=bool(binary),
        authenticated=True,
        status="ok" if windows or spending else "signed_in_no_data",
        source="Cursor dashboard",
        windows=windows,
        spending=spending,
        tokens={
            "unit": "tokens",
            "remaining": None,
            "note": "Cursor publishes a percentage, not an absolute token allowance",
        },
    )


_COLLECTORS = {"codex": _codex, "claude": _claude, "gemini": _gemini, "cursor": _cursor}


def collect(*, timeout=12.0, network=True):
    """Collect all provider limits concurrently without returning credentials or account IDs."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
        raise ValueError("provider timeout must be between 1 and 60 seconds")
    selected = PROVIDERS if network else ("claude",)
    results = {}
    with ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="usage") as pool:
        futures = {pool.submit(_COLLECTORS[name], float(timeout)): name for name in selected}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = _base(
                    name,
                    installed=bool(shutil.which(name)),
                    authenticated=False,
                    status="error",
                    source="provider collector",
                    error=exc,
                )
    if not network:
        for name in PROVIDERS:
            if name not in results:
                results[name] = _base(
                    name,
                    installed=bool(shutil.which(name)),
                    authenticated=False,
                    status="network_disabled",
                    source="not queried",
                )
    now = time.time()
    ordered = []
    with _CACHE_LOCK:
        for name in PROVIDERS:
            current = results[name]
            if current["status"] == "ok":
                _CACHE[name] = current
            elif name in _CACHE:
                cached = dict(_CACHE[name])
                cached["stale"] = True
                cached["status"] = "stale"
                cached["error"] = current.get("error") or current["status"]
                current = cached
            ordered.append(current)
    return {"protocol": PROTOCOL, "captured_at": now, "providers": ordered}
