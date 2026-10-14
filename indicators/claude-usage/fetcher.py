"""Claude Code usage fetcher. Solo stdlib.

Escribe ~/.cache/claude-usage.json con el MISMO contrato que el fetcher de
Copilot: percent_used, used, entitlement, remaining, reset, updated_at, stale.

Fuente primaria: GET https://api.anthropic.com/api/oauth/usage (endpoint
indocumentado que alimenta el /usage de Claude Code).
  Headers: Authorization: Bearer <token>, anthropic-beta: oauth-2025-04-20,
           Accept: application/json, User-Agent: claude-code/<version>
  (el User-Agent es obligatorio: sin el, el endpoint responde 429.)

Tokens, en orden, todos locales y solo en memoria (nunca se imprimen ni se
escriben a disco):
  1. ~/.claude/.credentials.json -> claudeAiOauth.accessToken, solo si no
     esta vencido (expiresAt).
  2. CLAUDE_CODE_OAUTH_TOKEN del entorno de los procesos claude vivos
     (Claude Desktop refresca ese token y lo inyecta al CLI).

Fallback: estimacion rolling de 5h desde ~/.claude/projects/**/*.jsonl
(tokens de las ultimas 5h / CLAUDE_USAGE_5H_BUDGET_TOKENS), siempre stale.

Rate limit: el endpoint limita por access token, asi que solo se consulta la
red si el cache tiene mas de CLAUDE_USAGE_MIN_INTERVAL segundos (120 por
defecto) o esta stale; un 429 activa backoff en ~/.cache/claude-usage.next.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
FALLBACK_USER_AGENT = "claude-code/2.1.281"

# Refresh OAuth del token de Claude Code (datos verificados).
OAUTH_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_ACCOUNT = os.environ.get("USER") or "claude-code"

CRED_PATH = Path.home() / ".claude" / ".credentials.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_BIN = Path.home() / ".local" / "share" / "claude" / "versions"
CLAUDE_DESKTOP_BIN = Path.home() / ".config" / "Claude" / "claude-code"
CACHE_PATH = Path.home() / ".cache" / "claude-usage.json"
BACKOFF_PATH = Path.home() / ".cache" / "claude-usage.next"

MIN_INTERVAL = int(os.environ.get("CLAUDE_USAGE_MIN_INTERVAL", "120"))
BACKOFF_INTERVAL = int(os.environ.get("CLAUDE_USAGE_BACKOFF_INTERVAL", "300"))
WINDOW_BUDGET_TOKENS = int(
    os.environ.get("CLAUDE_USAGE_5H_BUDGET_TOKENS", "88000000")
)
WINDOW_HOURS = 5


def _log(msg: str) -> None:
    """Motivos en stderr. Nunca recibe tokens."""
    sys.stderr.write("claude-usage fetcher: " + msg + "\n")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------- user agent
def _detect_user_agent() -> str:
    """Version real de Claude Code si se puede detectar barato (solo dirs)."""
    candidates: list[Path] = []
    for base in (CLAUDE_BIN, CLAUDE_DESKTOP_BIN):
        try:
            candidates.extend(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
    versions = []
    for path in candidates:
        parts = path.name.split(".")
        if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
            versions.append(tuple(int(p) for p in parts[:3]))
    if not versions:
        return FALLBACK_USER_AGENT
    return "claude-code/" + ".".join(str(x) for x in max(versions))


# ------------------------------------------------------------------- tokens
def _parse_oauth_json(data: object) -> tuple[str | None, str | None]:
    """Extrae accessToken del JSON OAuth y valida expiresAt.

    Mismo criterio de vencimiento que el archivo de credenciales: expiresAt en
    milisegundos; se considera vencido si vence en menos de 60s. Devuelve
    (token, None) si es util, o (None, razon) si no.
    """
    if not isinstance(data, dict):
        return None, "JSON OAuth no es un objeto"
    oauth = data.get("claudeAiOauth") or {}
    if not isinstance(oauth, dict):
        return None, "JSON OAuth sin claudeAiOauth"
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None, "OAuth sin claudeAiOauth.accessToken"
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000 < time.time() + 60:
        return None, "accessToken vencido (expiresAt)"
    return token, None


def _read_keychain_secret() -> tuple[str | None, str | None]:
    """Lee el secreto desde el Keychain de macOS via `security`.

    Primero sin -a (aplica al usuario actual); si falla, reintenta con
    -a <usuario actual>. Timeout corto. Devuelve (secreto, None) o (None, razon).
    """
    commands = [
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-a", KEYCHAIN_ACCOUNT, "-w"],
    ]
    last_reason = "sin intentos de Keychain"
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
        except FileNotFoundError:
            return None, "comando security no disponible"
        except subprocess.TimeoutExpired:
            last_reason = "timeout leyendo Keychain"
            continue
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            last_reason = "Keychain fallo (exit %d): %s" % (proc.returncode, err or "sin detalle")
            continue
        secret = (proc.stdout or "").strip()
        if secret:
            return secret, None
        last_reason = "Keychain devolvio secreto vacio"
    return None, last_reason


def _write_keychain_secret(data: dict) -> bool:
    """Persiste el JSON de credenciales en el Keychain (best effort).

    Reemplaza el item existente (-U). Si el write falla, solo loguea y sigue:
    el token nuevo en memoria sigue siendo util para este ciclo.
    """
    blob = json.dumps(data)
    cmd = [
        "security", "add-generic-password", "-U",
        "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w", blob,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _log("no se pudo escribir el Keychain: " + type(exc).__name__)
        return False
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        _log("write Keychain fallo (exit %d): %s" % (proc.returncode, err or "sin detalle"))
        return False
    return True


def _oauth_refresh(refresh_token: str, base_data: dict) -> tuple[str | None, str | None]:
    """Refresca el accessToken via OAuth usando el refreshToken.

    POST a OAUTH_TOKEN_URL. Con 200 devuelve el nuevo access_token y persiste
    el JSON actualizado en el Keychain (conserva scopes, reemplaza tokens y
    expiresAt). Con cualquier fallo loguea la razon y devuelve (None, razon)
    sin crashear, para seguir el flujo de fallback actual.
    """
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:200]
        except OSError:
            pass
        return None, "refresh HTTP %d: %s" % (exc.code, detail or "sin detalle")
    except Exception as exc:  # red, DNS, timeout
        return None, "refresh fallo red: " + type(exc).__name__

    if status != 200:
        return None, "refresh HTTP %d (no 200)" % status
    try:
        token_data = json.loads(body)
    except json.JSONDecodeError:
        return None, "refresh respuesta no-JSON"
    if not isinstance(token_data, dict):
        return None, "refresh respuesta no es objeto"
    new_access = token_data.get("access_token")
    new_refresh = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")
    if not isinstance(new_access, str) or not new_access:
        return None, "refresh sin access_token"
    if not isinstance(expires_in, (int, float)):
        expires_in = 28800
    new_expires_at = int(time.time() * 1000) + int(expires_in) * 1000

    # Persistir en Keychain: conserva scopes y demas, reemplaza tokens/expiresAt.
    updated = dict(base_data) if isinstance(base_data, dict) else {}
    oauth = dict(updated.get("claudeAiOauth") or {})
    oauth["accessToken"] = new_access
    if isinstance(new_refresh, str) and new_refresh:
        oauth["refreshToken"] = new_refresh
    oauth["expiresAt"] = new_expires_at
    updated["claudeAiOauth"] = oauth
    _write_keychain_secret(updated)  # best effort; ya loguea si falla
    return new_access, None


def _keychain_token() -> tuple[str | None, str | None]:
    """Token desde el Keychain de macOS (2a fuente tras el archivo).

    Si el accessToken esta vencido o a punto de vencer, intenta el refresh
    OAuth con el refreshToken antes de rechazarlo. Si el refresh falla,
    loguea la razon y devuelve (None, razon) para seguir el fallback actual.
    """
    secret, reason = _read_keychain_secret()
    if secret is None:
        return None, reason
    try:
        data = json.loads(secret)
    except json.JSONDecodeError:
        return None, "JSON invalido en Keychain"
    token, err = _parse_oauth_json(data)
    if token is not None:
        return token, None

    # Token vencido (o a punto de vencer): intentar refresh OAuth.
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    refresh_token = oauth.get("refreshToken") if isinstance(oauth, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        return None, (err or "sin token") + " y sin refreshToken en Keychain"
    _log("accessToken del Keychain vencido; intentando refresh OAuth")
    token, refresh_err = _oauth_refresh(refresh_token, data)
    if token is not None:
        _log("refresh OAuth ok; accessToken nuevo persistido en Keychain")
        return token, None
    return None, (refresh_err or "refresh fallo") + " (" + (err or "vencido") + ")"


def _credentials_token() -> tuple[str | None, str | None]:
    """Token OAuth util: 1) archivo de credenciales, 2) Keychain macOS.

    Aplica la misma logica de vencimiento por expiresAt a ambas fuentes. Si
    ninguna da token vivo, devuelve (None, razon) y el caller cae al fallback.
    """
    # 1) Archivo ~/.claude/.credentials.json
    file_reason = "sin credentials"
    try:
        data = json.loads(CRED_PATH.read_text(encoding="utf-8"))
        token, err = _parse_oauth_json(data)
        if token is not None:
            return token, None
        file_reason = err or "credentials sin token util"
    except FileNotFoundError:
        file_reason = "no existe " + str(CRED_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        file_reason = "no se pudo leer credentials: " + type(exc).__name__

    # 2) Keychain de macOS (si el archivo no dio token vivo)
    _log("credentials archivo sin token (" + file_reason + "); probando Keychain")
    token, key_reason = _keychain_token()
    if token is not None:
        return token, None
    return None, (key_reason or "sin token") + " (archivo: " + file_reason + ")"


def _live_tokens() -> list[str]:
    """CLAUDE_CODE_OAUTH_TOKEN de los procesos claude vivos de este usuario.

    Solo lectura de /proc propio; el token nunca sale de memoria.
    """
    found: list[str] = []
    own = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if own:
        found.append(own.strip())
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                cmd = fh.read()
        except OSError:
            continue
        if b"claude" not in cmd.lower():
            continue
        try:
            with open("/proc/%s/environ" % pid, "rb") as fh:
                entries = fh.read().split(b"\0")
        except OSError:
            continue
        for entry in entries:
            if entry.startswith(b"CLAUDE_CODE_OAUTH_TOKEN="):
                value = entry.partition(b"=")[2].decode("utf-8", "ignore").strip()
                if value and value not in found:
                    found.append(value)
    return found


# --------------------------------------------------------------- http / parse
def _get_usage(token: str) -> tuple[int, str | None]:
    req = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": BETA_HEADER,
            "Accept": "application/json",
            "User-Agent": _detect_user_agent(),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except OSError:
            pass
        return exc.code, body
    except Exception as exc:  # red, DNS, timeout
        return 0, str(type(exc).__name__)


def _api_message(body: str | None) -> str:
    if not body:
        return ""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("type") or "") + ": " + str(err.get("message") or "")
    return ""


def parse_usage(data: dict) -> dict | None:
    """Del JSON del endpoint arma el contrato de cache. None si no hay datos."""
    now = _now()

    # 1) Ventana 5h (cuentas con rate limit por ventana): utilization + reset.
    five_hour = data.get("five_hour")
    if isinstance(five_hour, dict) and five_hour.get("utilization") is not None:
        percent = _clamp(float(five_hour.get("utilization") or 0))
        return {
            "percent_used": percent,
            "used": 0,
            "entitlement": 0,
            "remaining": round(100.0 - percent, 1),
            "reset": str(five_hour.get("resets_at") or ""),
            "updated_at": _iso(now),
            "stale": False,
        }

    # 2) Gasto extra (cuentas enterprise/usage-based): spend mensual en USD.
    spend = data.get("spend")
    if isinstance(spend, dict) and spend.get("enabled") and isinstance(spend.get("limit"), dict):
        used_minor = float((spend.get("used") or {}).get("amount_minor") or 0)
        limit_minor = float(spend.get("limit").get("amount_minor") or 0)
        exponent = int(spend.get("limit").get("exponent") or 2)
        if limit_minor > 0:
            factor = 10 ** exponent
            used = used_minor / factor
            limit = limit_minor / factor
            percent = spend.get("percent")
            percent = _clamp(float(percent) if percent is not None else used / limit * 100)
            return {
                "percent_used": percent,
                "used": round(used, 2),
                "entitlement": round(limit, 2),
                "remaining": round(limit - used, 2),
                "reset": "",
                "updated_at": _iso(now),
                "stale": False,
            }

    # 3) extra_usage como respaldo (mismo concepto, en creditos).
    extra = data.get("extra_usage")
    if isinstance(extra, dict) and extra.get("monthly_limit"):
        limit = float(extra.get("monthly_limit") or 0)
        used = float(extra.get("used_credits") or 0)
        if limit > 0:
            percent = extra.get("utilization")
            percent = _clamp(float(percent) if percent is not None else used / limit * 100)
            return {
                "percent_used": percent,
                "used": round(used, 2),
                "entitlement": round(limit, 2),
                "remaining": round(limit - used, 2),
                "reset": "",
                "updated_at": _iso(now),
                "stale": False,
            }

    return None


def _clamp(value: float) -> float:
    if value != value:  # NaN
        return 0.0
    if value < 0:
        return 0.0
    if value > 100:
        return 100.0
    return round(float(value), 1)


# --------------------------------------------------------------- jsonl fallback
def estimate_from_jsonl() -> dict | None:
    """Estimacion rolling 5h desde los transcripts locales de Claude Code."""
    if not PROJECTS_DIR.is_dir():
        return None
    now = _now()
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    window_start: datetime | None = None
    tokens = 0
    seen_any = False

    for path in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                continue
        except OSError:
            continue
        try:
            handle = path.open("r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                ts = _parse_iso(entry.get("timestamp") or "")
                if ts is None or ts < cutoff:
                    continue
                usage = (entry.get("message") or {}).get("usage") or {}
                if not isinstance(usage, dict):
                    continue
                seen_any = True
                tokens += int(usage.get("input_tokens") or 0)
                tokens += int(usage.get("output_tokens") or 0)
                tokens += int(usage.get("cache_creation_input_tokens") or 0)
                if window_start is None or ts < window_start:
                    window_start = ts

    budget = max(WINDOW_BUDGET_TOKENS, 1)
    percent = _clamp(tokens / budget * 100)
    used = round(tokens / 1_000_000, 2)
    entitlement = round(budget / 1_000_000, 2)
    reset = ""
    if seen_any and window_start is not None:
        reset = _iso(window_start + timedelta(hours=WINDOW_HOURS))
    return {
        "percent_used": percent,
        "used": used,
        "entitlement": entitlement,
        "remaining": round(entitlement - used, 2),
        "reset": reset,
        "updated_at": _iso(now),
        "stale": True,
    }


# ----------------------------------------------------------------- cache io
def _read_cache() -> dict | None:
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as fh:
            cached = json.load(fh)
        return cached if isinstance(cached, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_cache_atomic(payload: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(CACHE_PATH.parent), prefix=".claude-usage.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, CACHE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_backoff() -> float:
    try:
        return float(BACKOFF_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def _write_backoff(until_ts: float) -> None:
    try:
        BACKOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKOFF_PATH.write_text(str(until_ts), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------- main
def get_usage() -> dict:
    """Obtiene uso de Claude y escribe el cache atomico. Nunca expone tokens."""
    now_ts = time.time()
    cached = _read_cache()

    # Cache util y fresco: no toca la red (el endpoint limita por token).
    if cached is not None and not cached.get("stale"):
        updated = _parse_iso(cached.get("updated_at") or "")
        if updated is not None and (now_ts - updated.timestamp()) < MIN_INTERVAL:
            return cached

    backoff = _read_backoff()
    if cached is not None and backoff > now_ts:
        cached["stale"] = True
        return cached

    token, cred_reason = _credentials_token()
    candidates: list[str] = []
    if token:
        candidates.append(token)
    else:
        _log(cred_reason or "sin token en credentials")
    candidates.extend(_live_tokens())

    last_reason = cred_reason or "sin token disponible"
    if not candidates:
        _log("sin token utilizable; usando estimacion JSONL rolling %dh" % WINDOW_HOURS)
    for candidate in candidates:
        status, body = _get_usage(candidate)
        if status == 200:
            try:
                payload = parse_usage(json.loads(body or "{}"))
            except json.JSONDecodeError:
                payload = None
                last_reason = "respuesta no-JSON del endpoint"
            if payload is not None:
                _write_cache_atomic(payload)
                _write_backoff(0.0)
                return payload
            last_reason = "endpoint sin datos de uso utilizables"
            break
        if status == 401 or status == 403:
            last_reason = "token rechazado (%d) %s" % (status, _api_message(body))
            continue
        if status == 429:
            last_reason = "rate limit 429 del endpoint"
            _write_backoff(now_ts + BACKOFF_INTERVAL)
            break
        last_reason = "HTTP %d %s" % (status, _api_message(body))
        _write_backoff(now_ts + BACKOFF_INTERVAL)
        break

    if candidates and last_reason:
        _log(last_reason)

    estimate = estimate_from_jsonl()
    if estimate is not None:
        _log(
            "auth no disponible; cache stale con estimacion JSONL rolling %dh"
            % WINDOW_HOURS
        )
        _write_cache_atomic(estimate)
        return estimate

    if cached is not None:
        cached["stale"] = True
        _write_cache_atomic(cached)
        _log("auth no disponible y sin transcripts; cache previo marcado stale")
        return cached

    payload = {
        "percent_used": 0.0,
        "used": 0,
        "entitlement": 0,
        "remaining": 0,
        "reset": "",
        "updated_at": _iso(_now()),
        "stale": True,
    }
    _write_cache_atomic(payload)
    _log("auth no disponible y sin cache previo; cache en ceros stale")
    return payload


if __name__ == "__main__":
    print(json.dumps(get_usage()))
