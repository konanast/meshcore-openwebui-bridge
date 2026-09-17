import asyncio
import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

import httpx
from meshcore import MeshCore, EventType

from conversation import ConversationService, ConversationStore, OpenWebUIChatClient


def env_bool(name, default):
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


SERIAL = os.getenv("MESHCORE_SERIAL", "/dev/ttyACM0")
BAUD = env_int("MESHCORE_BAUD", 115200)
DEBUG = env_bool("MESHCORE_DEBUG", False)

DIRECT = env_bool("ENABLE_DIRECT_MESSAGES", True)
CHANNEL = env_bool("ENABLE_CHANNEL_MESSAGES", True)
PUBLIC_CHANNEL = env_bool("ENABLE_PUBLIC_CHANNEL_MESSAGES", False)
CHANNEL_REQUIRE_MENTION = env_bool("CHANNEL_REQUIRE_MENTION", True)
CHANNEL_MENTION = os.getenv("CHANNEL_MENTION", "@ai").strip()

WAIT_ACK = env_bool("WAIT_FOR_DIRECT_ACK", True)
ACK_TIMEOUT = max(1.0, env_float("ACK_TIMEOUT_SECONDS", 12))
DIRECT_RETRIES = max(1, env_int("DIRECT_SEND_RETRIES", 2))

OWUI = os.getenv("OPENWEBUI_URL", "http://host.docker.internal:3000").rstrip("/")
KEY = os.getenv("OPENWEBUI_API_KEY", "").strip()

FAST = os.getenv("FAST_MODEL", "Qwen3-0.6B-GGUF").strip()
ASK = os.getenv("ASK_MODEL", FAST).strip()
DEEP = os.getenv("DEEP_MODEL", FAST).strip()

AI_PREFIX = os.getenv("AI_PREFIX", "AI:").strip()
MAXTOK = env_int("MAX_OUTPUT_TOKENS", 180)
MAXCHARS = env_int("MAX_REPLY_CHARS", 700)
SHORTTOK = env_int("SHORT_OUTPUT_TOKENS", 80)
SHORTCHARS = env_int("SHORT_REPLY_CHARS", 220)
DISABLE_THINKING_FAST = env_bool("DISABLE_THINKING_FAST", True)
DISABLE_THINKING_ASK = env_bool("DISABLE_THINKING_ASK", True)
DISABLE_THINKING_DEEP = env_bool("DISABLE_THINKING_DEEP", False)
EMPTY_RESPONSE_RETRY = env_bool("EMPTY_RESPONSE_RETRY", True)
EMPTY_RESPONSE_RETRY_TOKENS = env_int("EMPTY_RESPONSE_RETRY_TOKENS", 512)
TEMP = env_float("TEMPERATURE", 0.2)
TIMEOUT = env_float("REQUEST_TIMEOUT_SECONDS", 120)

HISTORY = env_bool("CHAT_HISTORY_ENABLED", True)
HISTORY_DB = os.getenv("CHAT_HISTORY_DATABASE", "/data/history.sqlite3")
HISTORY_TURNS = max(1, env_int("CHAT_HISTORY_MAX_TURNS", 8))
HISTORY_CHARS = max(256, env_int("CHAT_HISTORY_MAX_CHARS", 8000))
CHAT_FOLDER = os.getenv("OPENWEBUI_CHAT_FOLDER", "meshcore").strip()

CHUNK = max(50, min(env_int("MESH_CHUNK_CHARS", 120), 125))
CHUNK_DELAY = max(0.0, env_float("CHUNK_DELAY_SECONDS", 1.0))
TTL = max(30, env_int("DEDUPE_TTL_SECONDS", 300))

ALLOWED_CHANNELS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_CHANNELS", "").split(",")
    if x.strip().isdigit()
}
PUBLIC_CHANNELS = {
    int(x.strip())
    for x in os.getenv("PUBLIC_CHANNELS", "0").split(",")
    if x.strip().isdigit()
}
ALLOWED_PREFIXES = {
    x.strip().lower()
    for x in os.getenv("ALLOWED_CONTACT_PREFIXES", "").split(",")
    if x.strip()
}

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("meshcore-openwebui")

STARTED = time.monotonic()
request_sem = asyncio.Semaphore(max(1, env_int("MAX_CONCURRENT_REQUESTS", 1)))
seen = OrderedDict()
contacts = {}
conversation_service = None

last_llm = {
    "when": None,
    "model": None,
    "ok": None,
    "latency_ms": None,
    "error": None,
}
last_tx = {
    "when": None,
    "kind": None,
    "destination": None,
    "chunks": 0,
    "local_ok": None,
    "acked": None,
    "ack_code": None,
    "error": None,
}


def uptime_text():
    s = int(time.monotonic() - STARTED)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    if m or h or d:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def age_text(t):
    if t is None:
        return "never"
    s = int(time.monotonic() - t)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


def route(text):
    t = text.strip()
    low = t.lower()

    if low in {"/info", "/help"}:
        return "__info__", "", False
    if low == "/status":
        return "__status__", "", False
    if low == "/ping":
        return "__ping__", "", False
    if low == "/models":
        return "__models__", "", False
    if low == "/uptime":
        return "__uptime__", "", False
    if low == "/last":
        return "__last__", "", False
    if low == "/reset":
        return "__new__", "", False
    if low == "/new":
        return "__new__", "", False
    if low == "/history":
        return "__history__", "", False
    if low == "/diag":
        return "__diag__", "", False

    if low.startswith("/short "):
        return FAST, t[7:].strip(), True
    if low == "/short":
        return "__info__", "", False

    if low.startswith("/temp "):
        return "__temp__", t[6:].strip(), False
    if low == "/temp":
        return "__info__", "", False

    for cmd, model in (("/fast", FAST), ("/ask", ASK), ("/deep", DEEP)):
        if low.startswith(cmd + " "):
            return model, t[len(cmd) :].strip(), False
        if low == cmd:
            return "__info__", "", False

    # Dedicated gateway behavior: plain messages use the fast model.
    return FAST, t, False


def is_duplicate(kind, ident, ts, text):
    now = time.monotonic()
    while seen:
        _, old = next(iter(seen.items()))
        if now - old <= TTL:
            break
        seen.popitem(last=False)

    digest = hashlib.sha256(
        f"{kind}|{ident}|{ts}|{text}".encode("utf-8", errors="replace")
    ).hexdigest()

    if digest in seen:
        return True

    seen[digest] = now
    if len(seen) > 2048:
        seen.popitem(last=False)
    return False


def split_chunks(text, maxchars=None):
    maxchars = MAXCHARS if maxchars is None else maxchars
    text = normalized_reply(text, maxchars)

    limit = max(40, CHUNK - len(AI_PREFIX) - 8)
    out, cur = [], ""

    for word in text.split():
        while len(word) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(word[:limit])
            word = word[limit:]

        candidate = word if not cur else cur + " " + word
        if len(candidate) <= limit:
            cur = candidate
        else:
            out.append(cur)
            cur = word

    if cur:
        out.append(cur)
    if not out:
        out = ["(no response)"]

    if len(out) == 1:
        return [f"{AI_PREFIX} {out[0]}".strip()]

    n = len(out)
    return [f"{AI_PREFIX} [{idx}/{n}] {part}" for idx, part in enumerate(out, 1)]


def normalized_reply(text, maxchars):
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) > maxchars:
        text = text[: maxchars - 1].rstrip() + "…"
    return text


def channel_prompt(text):
    """Return a channel prompt with its mention removed, or None if not addressed."""
    text = text.strip()
    if not CHANNEL_REQUIRE_MENTION:
        return text
    if not CHANNEL_MENTION:
        return None
    pattern = rf"(?<!\w){re.escape(CHANNEL_MENTION)}(?!\w)"
    if not re.search(pattern, text, flags=re.IGNORECASE):
        return None
    return re.sub(pattern, "", text, count=1, flags=re.IGNORECASE).strip()


def channel_index_allowed(channel_idx):
    if channel_idx in PUBLIC_CHANNELS and not PUBLIC_CHANNEL:
        return False
    return not ALLOWED_CHANNELS or channel_idx in ALLOWED_CHANNELS


@asynccontextmanager
async def _null_async_context():
    yield


async def refresh_contacts(mc):
    global contacts
    r = await mc.commands.get_contacts()
    if r.type != EventType.ERROR:
        contacts = r.payload or {}
        log.info("Loaded %d contacts", len(contacts))
    else:
        log.warning("get_contacts failed: %s", r.payload)


def contact_for(mc, prefix):
    try:
        c = mc.get_contact_by_key_prefix(prefix)
        if c:
            return c
    except Exception:
        pass

    for c in contacts.values():
        if str(c.get("public_key", "")).lower().startswith(prefix.lower()):
            return c
    return None


def contact_identity(contact, prefix):
    """Return a stable conversation key and a human-friendly chat title."""
    public_key_value = contact.get("public_key", "")
    if isinstance(public_key_value, (bytes, bytearray)):
        public_key = bytes(public_key_value).hex()
    else:
        public_key = str(public_key_value).strip().lower()
    node_key = public_key or prefix.lower()
    name = str(
        contact.get("adv_name")
        or contact.get("name")
        or contact.get("display_name")
        or "Node"
    ).strip()
    suffix = node_key[:12] if node_key else prefix[:12]
    return node_key, f"MeshCore - {name} - {suffix}"


async def probe_openwebui():
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=min(TIMEOUT, 8.0)) as c:
            r = await c.get(OWUI + "/")
        return True, int((time.monotonic() - started) * 1000), r.status_code
    except Exception as e:
        return False, int((time.monotonic() - started) * 1000), type(e).__name__


async def get_mesh_stats(mc):
    result = {}
    for key, method_name in (
        ("core", "get_stats_core"),
        ("radio", "get_stats_radio"),
        ("packets", "get_stats_packets"),
    ):
        try:
            method = getattr(mc.commands, method_name)
            r = await method()
            result[key] = None if r.type == EventType.ERROR else r.payload
        except Exception as e:
            result[key] = {"error": type(e).__name__}
    return result


def compact_dict(d, keys):
    if not isinstance(d, dict):
        return ""
    vals = []
    for k in keys:
        if k in d:
            vals.append(f"{k}={d[k]}")
    return ",".join(vals)


def info_text():
    return (
        "Commands: plain=/fast; /fast <q>; /ask <q>; /deep <q>; "
        "/short <q>; /ping; /status; /diag; /models; /uptime; "
        "/last; /history; /new; /temp <q>; /reset; /info."
    )


def models_text():
    return f"Routes: fast={FAST}; ask={ASK}; deep={DEEP}; short={FAST}"


def last_text():
    if last_llm["when"] is None and last_tx["when"] is None:
        return "Last: no AI requests or radio replies since bridge start."

    llm = "LLM=none"
    if last_llm["when"] is not None:
        llm_state = "ok" if last_llm["ok"] else "ERR"
        llm = (
            f"LLM={last_llm['model']} {llm_state} "
            f"{last_llm['latency_ms']}ms {age_text(last_llm['when'])}"
        )

    tx = "TX=none"
    if last_tx["when"] is not None:
        if last_tx["kind"] == "direct":
            delivery = (
                "ACK"
                if last_tx["acked"]
                else ("NO-ACK" if last_tx["acked"] is False else "local-only")
            )
        else:
            delivery = "channel-local-ok" if last_tx["local_ok"] else "ERR"
        tx = (
            f"TX={last_tx['kind']}->{last_tx['destination']} "
            f"{delivery} chunks={last_tx['chunks']} {age_text(last_tx['when'])}"
        )

    return f"Last: {llm}; {tx}"


async def local_command(mc, name):
    if name == "__info__":
        return info_text()
    if name == "__models__":
        return models_text()
    if name == "__uptime__":
        return f"Uptime: bridge={uptime_text()} serial={SERIAL}@{BAUD}"
    if name == "__last__":
        return last_text()
    if name == "__reset__":
        seen.clear()
        last_llm.update(when=None, model=None, ok=None, latency_ms=None, error=None)
        last_tx.update(
            when=None,
            kind=None,
            destination=None,
            chunks=0,
            local_ok=None,
            acked=None,
            ack_code=None,
            error=None,
        )
        return "Reset: bridge diagnostics/dedupe cleared. Use /new to start a new conversation."

    if name == "__ping__":
        ok, ms, status = await probe_openwebui()
        return (
            f"PONG bridge=ok meshcore={'ok' if getattr(mc, 'is_connected', True) else 'DOWN'} "
            f"openwebui={'ok' if ok else 'DOWN'} detail={status} {ms}ms"
        )

    if name == "__status__":
        ok, ms, status = await probe_openwebui()
        return (
            f"Status: bridge=ok uptime={uptime_text()}; "
            f"meshcore={'ok' if getattr(mc, 'is_connected', True) else 'DOWN'} {SERIAL}; "
            f"openwebui={'ok' if ok else 'DOWN'}({status},{ms}ms); "
            f"{last_text()}"
        )

    if name == "__diag__":
        ok, ms, status = await probe_openwebui()
        stats = await get_mesh_stats(mc)

        core = compact_dict(
            stats.get("core"), ["uptime", "battery_mv", "queue_len", "errors"]
        )
        radio = compact_dict(
            stats.get("radio"),
            ["noise_floor", "last_rssi", "last_snr", "tx_time", "rx_time"],
        )
        packets = compact_dict(
            stats.get("packets"),
            ["rx_total", "tx_total", "recv_errors", "flood_rx", "direct_rx"],
        )

        parts = [
            f"DIAG bridge=ok/{uptime_text()}",
            f"serial={'ok' if getattr(mc, 'is_connected', True) else 'DOWN'}",
            f"owui={'ok' if ok else 'DOWN'}({status},{ms}ms)",
        ]
        if core:
            parts.append("core:" + core)
        if radio:
            parts.append("radio:" + radio)
        if packets:
            parts.append("pkt:" + packets)
        parts.append(last_text())
        return "; ".join(parts)

    return "Unknown local command"


def disable_thinking_for(model, short=False):
    if short:
        return True
    if model == FAST:
        return DISABLE_THINKING_FAST
    if model == ASK:
        return DISABLE_THINKING_ASK
    if model == DEEP:
        return DISABLE_THINKING_DEEP
    return False


def extract_visible_content(data):
    try:
        choice = data["choices"][0]
        msg = choice.get("message", {}) or {}
        finish = choice.get("finish_reason")
    except Exception:
        return "", None, 0

    content = msg.get("content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        content = " ".join(parts)
    elif content is None:
        content = ""

    reasoning = (
        msg.get("reasoning_content")
        or msg.get("reasoning")
        or msg.get("thinking")
        or ""
    )
    if isinstance(reasoning, list):
        reasoning = " ".join(str(x) for x in reasoning)

    return str(content).strip(), finish, len(str(reasoning))


async def _openwebui_completion(
    model, q, short=False, force_no_think=False, token_override=None, history=None
):
    maxchars = SHORTCHARS if short else MAXCHARS
    maxtok = (
        token_override
        if token_override is not None
        else (SHORTTOK if short else MAXTOK)
    )

    style = (
        f"Reply over low-bandwidth MeshCore LoRa. Keep the entire answer under "
        f"{maxchars} characters. Be concise, factual, directly useful, and avoid "
        f"markdown tables or unnecessary preambles."
    )
    if short:
        style += " This is /short mode: prefer one or two compact sentences."

    no_think = force_no_think or disable_thinking_for(model, short=short)
    user_text = q

    # Lemonade documents /no_think for supported Qwen reasoning models.
    # Also request template-level no-thinking for compatible backends.
    if no_think and "/no_think" not in user_text:
        user_text = user_text.rstrip() + "\n/no_think"

    messages = [{"role": "system", "content": style}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": TEMP,
        "max_tokens": maxtok,
    }

    if no_think:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(
            OWUI + "/api/chat/completions",
            headers={
                "Authorization": f"Bearer {KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        return r.json()


async def ask_openwebui(model, q, short=False, history=None):
    if not KEY or KEY == "REPLACE_ME":
        raise RuntimeError("OPENWEBUI_API_KEY not configured")

    started = time.monotonic()
    last_llm.update(when=started, model=model, ok=None, latency_ms=None, error=None)

    try:
        data = await _openwebui_completion(model, q, short=short, history=history)
        content, finish, reasoning_len = extract_visible_content(data)

        log.info(
            "Open WebUI response model=%s finish_reason=%s visible_chars=%d reasoning_chars=%d",
            model,
            finish,
            len(content),
            reasoning_len,
        )

        if not content and EMPTY_RESPONSE_RETRY:
            log.warning(
                "Empty visible content from %s; retrying with thinking disabled and max_tokens=%d",
                model,
                EMPTY_RESPONSE_RETRY_TOKENS,
            )
            data = await _openwebui_completion(
                model,
                q,
                short=short,
                force_no_think=True,
                token_override=max(
                    EMPTY_RESPONSE_RETRY_TOKENS,
                    SHORTTOK if short else MAXTOK,
                ),
                history=history,
            )
            content, finish, reasoning_len = extract_visible_content(data)
            log.info(
                "Retry response model=%s finish_reason=%s visible_chars=%d reasoning_chars=%d",
                model,
                finish,
                len(content),
                reasoning_len,
            )

        if not content:
            raise RuntimeError(
                f"empty visible response: finish_reason={finish}, reasoning_chars={reasoning_len}"
            )

        last_llm["ok"] = True
        return content

    except Exception as e:
        last_llm["ok"] = False
        last_llm["error"] = f"{type(e).__name__}: {e}"
        raise

    finally:
        last_llm["latency_ms"] = int((time.monotonic() - started) * 1000)


def ack_code_from_result(result):
    try:
        ack = result.payload.get("expected_ack")
        if isinstance(ack, bytes):
            return ack.hex()
        if isinstance(ack, bytearray):
            return bytes(ack).hex()
        if ack is not None:
            return str(ack)
    except Exception:
        pass
    return None


async def send_direct_one(mc, contact, text):
    """
    Returns (local_ok, acked, ack_code, error).
    local_ok means the companion accepted the send command.
    acked=True means the remote MeshCore node acknowledged this exact message.
    """
    last_error = None

    for attempt in range(1, DIRECT_RETRIES + 1):
        r = await mc.commands.send_msg(contact, text)
        if r.type == EventType.ERROR:
            last_error = f"send error: {r.payload}"
            log.warning("Direct TX attempt %d failed locally: %s", attempt, r.payload)
            continue

        ack_code = ack_code_from_result(r)
        log.info("Direct TX accepted locally; expected_ack=%s", ack_code)

        if not WAIT_ACK or not ack_code:
            return True, None, ack_code, None

        ack = await mc.wait_for_event(
            EventType.ACK,
            attribute_filters={"code": ack_code},
            timeout=ACK_TIMEOUT,
        )

        if ack:
            log.info("Direct TX delivered: ACK %s received", ack_code)
            return True, True, ack_code, None

        last_error = f"ACK timeout ({ACK_TIMEOUT}s)"
        log.warning(
            "Direct TX attempt %d/%d had no remote ACK (%s)",
            attempt,
            DIRECT_RETRIES,
            ack_code,
        )

    return True, False, ack_code if "ack_code" in locals() else None, last_error


async def send_direct(mc, contact, destination, text, maxchars=None):
    parts = split_chunks(text, maxchars=maxchars)
    last_tx.update(
        when=time.monotonic(),
        kind="direct",
        destination=destination,
        chunks=len(parts),
        local_ok=True,
        acked=True if WAIT_ACK else None,
        ack_code=None,
        error=None,
    )

    all_acked = True if WAIT_ACK else None

    for part in parts:
        log.info("TX direct: %s", part)
        local_ok, acked, ack_code, err = await send_direct_one(mc, contact, part)
        last_tx["ack_code"] = ack_code

        if not local_ok:
            last_tx["local_ok"] = False
            last_tx["error"] = err
            raise RuntimeError(err or "direct send failed")

        if WAIT_ACK and acked is not True:
            all_acked = False
            last_tx["error"] = err

        if CHUNK_DELAY:
            await asyncio.sleep(CHUNK_DELAY)

    last_tx["acked"] = all_acked


async def send_channel(mc, channel_idx, text, maxchars=None):
    parts = split_chunks(text, maxchars=maxchars)
    last_tx.update(
        when=time.monotonic(),
        kind="channel",
        destination=str(channel_idx),
        chunks=len(parts),
        local_ok=True,
        acked=None,
        ack_code=None,
        error=None,
    )

    for part in parts:
        log.info("TX channel %s: %s", channel_idx, part)
        r = await mc.commands.send_chan_msg(channel_idx, part)
        if r.type == EventType.ERROR:
            last_tx["local_ok"] = False
            last_tx["error"] = str(r.payload)
            raise RuntimeError(f"channel send failed: {r.payload}")
        if CHUNK_DELAY:
            await asyncio.sleep(CHUNK_DELAY)


async def handle_direct(mc, event):
    p = event.payload or {}
    text = str(p.get("text", "")).strip()
    prefix = str(p.get("pubkey_prefix", "")).lower()

    if not text or (AI_PREFIX and text.startswith(AI_PREFIX)):
        return
    if ALLOWED_PREFIXES and not any(prefix.startswith(x) for x in ALLOWED_PREFIXES):
        return
    if is_duplicate("direct", prefix, p.get("timestamp", ""), text):
        return

    target, q, short = route(text)
    log.info("RX direct %s -> %s: %s", prefix, target, q)

    async with request_sem:
        contact = contact_for(mc, prefix)
        if not contact:
            await refresh_contacts(mc)
            contact = contact_for(mc, prefix)
        if not contact:
            log.error("Cannot resolve sender %s", prefix)
            return

        node_key, chat_title = contact_identity(contact, prefix)
        lock = (
            conversation_service.lock(node_key)
            if HISTORY and conversation_service
            else _null_async_context()
        )
        async with lock:
            generated = None
            model_used = None
            persistent_question = None
            try:
                if target == "__new__":
                    if not HISTORY or not conversation_service:
                        answer = "Chat history is disabled."
                    else:
                        await conversation_service.new_chat(node_key, chat_title, FAST)
                        answer = "New persistent conversation started."
                    maxchars = MAXCHARS
                elif target == "__history__":
                    status = (
                        await conversation_service.status(node_key)
                        if HISTORY and conversation_service
                        else None
                    )
                    answer = (
                        f"History: {status['turns']} turns; chat={status['chat_id'][:12]}…"
                        if status
                        else "History: no persistent conversation yet."
                    )
                    maxchars = MAXCHARS
                elif target == "__temp__":
                    answer = await ask_openwebui(FAST, q)
                    maxchars = MAXCHARS
                elif target.startswith("__"):
                    answer = await local_command(mc, target)
                    maxchars = MAXCHARS
                else:
                    history = []
                    if HISTORY and conversation_service:
                        await conversation_service.ensure_chat(
                            node_key, chat_title, target
                        )
                        history = await conversation_service.context(node_key)
                    answer = await ask_openwebui(
                        target, q, short=short, history=history
                    )
                    maxchars = SHORTCHARS if short else MAXCHARS
                    generated = answer
                    model_used = target
                    persistent_question = q
            except Exception as e:
                log.exception("Request failed")
                answer = f"Service error: {type(e).__name__}"
                maxchars = MAXCHARS

            delivery = "failed"
            try:
                await send_direct(mc, contact, prefix, answer, maxchars=maxchars)
                delivery = (
                    "acked"
                    if last_tx["acked"] is True
                    else "local-only"
                    if not WAIT_ACK and last_tx["local_ok"]
                    else "unacked"
                    if last_tx["local_ok"]
                    else "failed"
                )
            except Exception:
                log.exception("MeshCore direct reply failed")

            if generated is not None and HISTORY and conversation_service:
                try:
                    await conversation_service.record_and_sync(
                        node_key,
                        persistent_question,
                        normalized_reply(generated, maxchars),
                        model_used,
                        delivery,
                    )
                except Exception:
                    log.exception(
                        "Could not persist/synchronize conversation for %s", prefix
                    )


async def handle_channel(mc, event):
    p = event.payload or {}
    text = str(p.get("text", "")).strip()

    try:
        ch = int(p.get("channel_idx", -1))
    except Exception:
        return

    if not text or ch < 0 or (AI_PREFIX and text.startswith(AI_PREFIX)):
        return
    if not channel_index_allowed(ch):
        log.debug("Ignoring disabled or disallowed channel %s", ch)
        return
    text = channel_prompt(text)
    if not text:
        log.debug("Ignoring channel %s message without mention %r", ch, CHANNEL_MENTION)
        return
    if is_duplicate("channel", str(ch), p.get("timestamp", ""), text):
        return

    target, q, short = route(text)
    log.info("RX channel %s -> %s: %s", ch, target, q)

    async with request_sem:
        try:
            if target in {"__new__", "__history__"}:
                answer = "Persistent history is available for direct messages only."
                maxchars = MAXCHARS
            elif target == "__temp__":
                answer = await ask_openwebui(FAST, q)
                maxchars = MAXCHARS
            elif target.startswith("__"):
                answer = await local_command(mc, target)
                maxchars = MAXCHARS
            else:
                answer = await ask_openwebui(target, q, short=short)
                maxchars = SHORTCHARS if short else MAXCHARS
        except Exception as e:
            log.exception("Request failed")
            answer = f"Service error: {type(e).__name__}"
            maxchars = MAXCHARS

        try:
            await send_channel(mc, ch, answer, maxchars=maxchars)
        except Exception:
            log.exception("MeshCore channel reply failed")


async def session():
    log.info("Connecting to %s @ %s", SERIAL, BAUD)
    mc = await MeshCore.create_serial(SERIAL, BAUD, debug=DEBUG)
    await refresh_contacts(mc)

    if DIRECT:

        async def direct_cb(event):
            asyncio.create_task(handle_direct(mc, event))

        mc.subscribe(EventType.CONTACT_MSG_RECV, direct_cb)

    if CHANNEL:

        async def channel_cb(event):
            asyncio.create_task(handle_channel(mc, event))

        mc.subscribe(EventType.CHANNEL_MSG_RECV, channel_cb)

    # Useful global ACK log for troubleshooting.
    async def ack_cb(event):
        log.info("MeshCore ACK event: %s", event.payload)

    mc.subscribe(EventType.ACK, ack_cb)

    await mc.start_auto_message_fetching()

    log.info(
        "Ready. default/fast=%s ask=%s deep=%s | "
        "local: /info /ping /status /diag /models /uptime /last /history /new "
        "/reset /temp <q> /short <q>",
        FAST,
        ASK,
        DEEP,
    )
    log.info(
        "Channel policy: enabled=%s public=%s public_indexes=%s mention=%s required=%s",
        CHANNEL,
        PUBLIC_CHANNEL,
        sorted(PUBLIC_CHANNELS),
        CHANNEL_MENTION or "none",
        CHANNEL_REQUIRE_MENTION,
    )

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        try:
            await mc.stop_auto_message_fetching()
        except Exception:
            pass
        await mc.disconnect()


async def main():
    global conversation_service
    if not KEY or KEY == "REPLACE_ME":
        raise SystemExit("Set OPENWEBUI_API_KEY in .env")

    if HISTORY:
        store = ConversationStore(HISTORY_DB)
        await store.initialize()
        client = OpenWebUIChatClient(OWUI, KEY, TIMEOUT, CHAT_FOLDER)
        conversation_service = ConversationService(
            store, client, HISTORY_TURNS, HISTORY_CHARS
        )
        log.info(
            "Persistent direct-message history enabled: db=%s turns=%d chars=%d folder=%s",
            HISTORY_DB,
            HISTORY_TURNS,
            HISTORY_CHARS,
            CHAT_FOLDER or "none",
        )

    delay = 2
    while True:
        try:
            await session()
            delay = 2
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Bridge session failed; retrying in %ss", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


if __name__ == "__main__":
    asyncio.run(main())
