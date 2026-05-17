"""
Redis helpers for TradingView MCP tools — shared contract with SaviourBOT.
"""
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

STATE_TTL_DEFAULT = 8 * 3600

BIAS_MAP = {
    "BULLISH": "LONG_ONLY",
    "BEARISH": "SHORT_ONLY",
    "NEUTRAL": "BOTH",
    "LONG": "LONG_ONLY",
    "SHORT": "SHORT_ONLY",
    "LONG_ONLY": "LONG_ONLY",
    "SHORT_ONLY": "SHORT_ONLY",
    "BOTH": "BOTH",
    "NO_TRADE": "NO_TRADE",
}

REGIME_MAP = {
    "TRENDING": "RANGING",
    "TRENDING_UP": "BULLISH",
    "TRENDING_DOWN": "BEARISH",
    "MEAN_REVERTING": "RANGING",
    "MEANREVERTING": "RANGING",
    "PANIC": "BEARISH",
    "BULLISH": "BULLISH",
    "BEARISH": "BEARISH",
    "RANGING": "RANGING",
}


def bot_symbol(raw: str) -> str:
    return raw.split(":")[-1] if ":" in raw else raw


def redis_client():
    import redis

    url = os.getenv("REDIS_URL", "").strip()
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", "6379"))
    if url:
        return redis.Redis.from_url(url, decode_responses=True, socket_timeout=10.0)
    if host.startswith("redis://") or host.startswith("rediss://"):
        return redis.Redis.from_url(host, decode_responses=True, socket_timeout=10.0)
    return redis.Redis(
        host=host, port=port, db=0, decode_responses=True, socket_timeout=10.0
    )


def normalize_confidence(value: Any) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.5
    if c > 1.0:
        c /= 100.0
    return max(0.0, min(1.0, c))


def normalize_bias(raw: Optional[str]) -> str:
    if not raw:
        return "NO_TRADE"
    return BIAS_MAP.get(str(raw).strip().upper().replace(" ", "_"), "NO_TRADE")


def normalize_regime(raw: Optional[str]) -> str:
    if not raw:
        return "RANGING"
    key = str(raw).strip().upper().replace(" ", "_").replace("-", "_")
    return REGIME_MAP.get(key, key if key in ("BULLISH", "BEARISH", "RANGING") else "RANGING")


def publish_live_state(
    symbol: str,
    state: Dict[str, Any],
    ttl_sec: int = STATE_TTL_DEFAULT,
) -> str:
    """Write claude:live_state:{symbol} with bot-compatible schema."""
    sym = bot_symbol(symbol)
    r = redis_client()
    now_iso = datetime.now(timezone.utc).isoformat()

    payload = {
        "symbol": sym,
        "regime": normalize_regime(state.get("regime")),
        "recommended_bias": normalize_bias(state.get("recommended_bias")),
        "confidence": normalize_confidence(state.get("confidence")),
        "reasoning": state.get("reasoning", ""),
        "market_structure": state.get("market_structure") or {"trend": "CHOP"},
        "chop_trap": state.get("chop_trap") or {
            "is_choppy": False,
            "is_trap_session": False,
            "severity": "LOW",
            "notes": "",
        },
        "position_review": state.get("position_review") or {"verdict": "NONE", "reason": ""},
        "source": state.get("source", "claude_supervisor"),
        "_published_at": now_iso,
    }

    r.setex(f"claude:live_state:{sym}", ttl_sec, json.dumps(payload))
    r.setex("claude:last_updated", ttl_sec, now_iso)
    r.setex(
        "claude:last_scan",
        ttl_sec,
        json.dumps({"timestamp": now_iso, "symbol": sym}),
    )
    return sym


def read_live_state(symbol: str) -> Optional[Dict[str, Any]]:
    sym = bot_symbol(symbol)
    raw = redis_client().get(f"claude:live_state:{sym}")
    if not raw:
        return None
    return json.loads(raw)


def _last_close(r, sym: str) -> Optional[float]:
    raw = r.get(f"state:{sym}:ohlcv")
    if not raw:
        return None
    try:
        bars = json.loads(raw)
        if bars:
            return float(bars[-1].get("close", 0))
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        pass
    return None


def _position_age_hours(entry_time: Optional[str]) -> Optional[float]:
    if not entry_time:
        return None
    try:
        et = datetime.fromisoformat(str(entry_time).replace("Z", "+00:00"))
        if et.tzinfo is None:
            et = et.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - et).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def _unrealized_pct(direction: str, entry: float, current: float) -> Optional[float]:
    if not entry or not current:
        return None
    if direction == "BUY":
        return ((current - entry) / entry) * 100.0
    return ((entry - current) / entry) * 100.0


def fetch_active_positions(symbol: str) -> Dict[str, Any]:
    """Read live + shadow positions from bot Redis keys."""
    sym = bot_symbol(symbol)
    r = redis_client()
    current_price = _last_close(r, sym)

    def _parse_positions(key: str, label: str) -> List[Dict[str, Any]]:
        raw = r.get(key)
        if not raw:
            return []
        try:
            trades = json.loads(raw)
        except json.JSONDecodeError:
            return []
        out = []
        for t in trades:
            if not t.get("is_open", True):
                continue
            direction = t.get("direction", "")
            entry = float(t.get("entry_price") or 0)
            pnl = _unrealized_pct(direction, entry, current_price) if current_price else None
            age_h = _position_age_hours(t.get("entry_time"))
            out.append({
                "trade_id": t.get("trade_id"),
                "kind": label,
                "direction": direction,
                "entry_price": entry,
                "entry_time": t.get("entry_time"),
                "age_hours": round(age_h, 2) if age_h is not None else None,
                "unrealized_pnl_pct": round(pnl, 3) if pnl is not None else None,
                "current_price": current_price,
                "stop_loss": t.get("stop_loss"),
                "is_filled": t.get("is_filled", False),
            })
        return out

    live = _parse_positions(f"state:{sym}:positions", "live")
    shadow = _parse_positions(f"state:{sym}:shadow_positions", "shadow")
    return {
        "symbol": sym,
        "current_price": current_price,
        "positions": live + shadow,
        "has_open": bool(live or shadow),
    }
