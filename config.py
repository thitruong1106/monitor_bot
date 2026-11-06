"""Load watches.yaml, webhooks and runtime settings."""
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

WATCHES_FILE = "watches.yaml"
WEBHOOKS_FILE = "webhooks.json"

TIER_INTERVALS = {
    "hot": 5,
    "normal": 15,
    "slow": 60,
}

PLATFORMS = ("shopify", "other")

DEFAULT_COOLDOWN_HOURS = 6.0
DEFAULT_MISSING_GRACE_PASSES = 1


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number, using %s", name, raw, default)
        return default


# Missed-run detector: warn when no successful pass for this long...
MISSED_RUN_MINUTES = _env_float("MISSED_RUN_MINUTES", 30.0)
# ...but post at most one warning per this many hours (gaps in between are summarised).
MISSED_RUN_WARN_EVERY_HOURS = _env_float("MISSED_RUN_WARN_EVERY_HOURS", 6.0)
# Drop product state not seen for this many days of successful checks.
PRUNE_AFTER_DAYS = _env_float("PRUNE_AFTER_DAYS", 30.0)


def _compile_patterns(patterns: list[str], watch_url: str, kind: str) -> list[re.Pattern]:
    compiled = []
    for pat in patterns:
        try:
            compiled.append(re.compile(pat, re.IGNORECASE))
        except re.error as exc:
            # Never crash the whole run on a typo; match the text literally instead.
            logger.error("Bad %s regex %r on %s (%s) — matching it literally", kind, pat, watch_url, exc)
            compiled.append(re.compile(re.escape(pat), re.IGNORECASE))
    return compiled


@dataclass
class Watch:
    url: str
    game: str
    tier: str
    type: str          # "category" or "product"
    retailer_name: str
    platform: str | None = None              # "shopify" / "other"; None = probe at runtime
    include: list[str] = field(default_factory=list)   # title must match at least one (if any)
    exclude: list[str] = field(default_factory=list)   # title must match none
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS
    missing_means_unavailable: bool = False
    missing_grace_passes: int = DEFAULT_MISSING_GRACE_PASSES

    def __post_init__(self):
        self._include_re = _compile_patterns(self.include, self.url, "include")
        self._exclude_re = _compile_patterns(self.exclude, self.url, "exclude")

    @property
    def interval_minutes(self) -> int:
        return TIER_INTERVALS.get(self.tier, 15)

    def title_passes(self, title: str) -> bool:
        title = title or ""
        if self._include_re and not any(r.search(title) for r in self._include_re):
            return False
        if any(r.search(title) for r in self._exclude_re):
            return False
        return True

    def apply_filters(self, products: list[dict]) -> list[dict]:
        if not self._include_re and not self._exclude_re:
            return products
        return [p for p in products if self.title_passes(p.get("title", ""))]

    @property
    def filters_fingerprint(self) -> str:
        """Changes whenever include/exclude change, so the watch re-baselines silently."""
        if not self.include and not self.exclude:
            return ""
        blob = json.dumps({"include": self.include, "exclude": self.exclude}, sort_keys=True)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def load_watches(path: str = WATCHES_FILE) -> list[Watch]:
    if not os.path.exists(path):
        logger.warning("watches.yaml not found at %s", path)
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    watches = []
    for entry in raw:
        platform = entry.get("platform")
        if platform is not None and platform not in PLATFORMS:
            logger.warning("Unknown platform %r for %s — will probe instead", platform, entry["url"])
            platform = None
        watches.append(Watch(
            url=entry["url"],
            game=entry["game"],
            tier=entry.get("tier", "normal"),
            type=entry.get("type", "category"),
            retailer_name=entry.get("retailer_name", "Unknown"),
            platform=platform,
            include=_as_list(entry.get("include")),
            exclude=_as_list(entry.get("exclude")),
            cooldown_hours=float(entry.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS)),
            missing_means_unavailable=bool(entry.get("missing_means_unavailable", False)),
            missing_grace_passes=max(1, int(entry.get("missing_grace_passes", DEFAULT_MISSING_GRACE_PASSES))),
        ))
    return watches


def watch_to_dict(w: Watch) -> dict:
    """Serialise a watch, writing optional fields only when they differ from the default."""
    data = {
        "url": w.url,
        "game": w.game,
        "tier": w.tier,
        "type": w.type,
        "retailer_name": w.retailer_name,
    }
    if w.platform:
        data["platform"] = w.platform
    if w.include:
        data["include"] = list(w.include)
    if w.exclude:
        data["exclude"] = list(w.exclude)
    if w.cooldown_hours != DEFAULT_COOLDOWN_HOURS:
        data["cooldown_hours"] = w.cooldown_hours
    if w.missing_means_unavailable:
        data["missing_means_unavailable"] = True
    if w.missing_grace_passes != DEFAULT_MISSING_GRACE_PASSES:
        data["missing_grace_passes"] = w.missing_grace_passes
    return data


def save_watches(watches: list[Watch], path: str = WATCHES_FILE) -> None:
    data = [watch_to_dict(w) for w in watches]
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def load_webhooks() -> dict[str, str]:
    """
    Load webhook map. Priority:
    1. WEBHOOKS_JSON env var (JSON string)
    2. webhooks.json file
    """
    env_val = os.environ.get("WEBHOOKS_JSON")
    if env_val:
        try:
            return json.loads(env_val)
        except json.JSONDecodeError:
            logger.error("WEBHOOKS_JSON env var is not valid JSON")

    if os.path.exists(WEBHOOKS_FILE):
        with open(WEBHOOKS_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                logger.error("webhooks.json is malformed")

    return {}


def get_webhook(webhooks: dict[str, str], game: str) -> str | None:
    return webhooks.get(game) or webhooks.get("_fallback")


_ROLE_ID_RE = re.compile(r"^\d{15,25}$")


def load_role_id() -> str | None:
    """
    Discord role to @mention on stock alerts, from the DISCORD_ROLE_ID env var
    (a GitHub secret in Actions). Only a numeric snowflake is accepted, so a bad
    value can never turn into an @everyone ping.
    """
    raw = os.environ.get("DISCORD_ROLE_ID", "").strip()
    if not raw:
        return None
    if not _ROLE_ID_RE.match(raw):
        logger.warning("DISCORD_ROLE_ID is not a numeric Discord role ID — ignoring it")
        return None
    return raw


def load_heartbeat_url() -> str | None:
    """Optional dead-man's-switch URL (e.g. healthchecks.io) pinged after every good pass."""
    raw = os.environ.get("HEARTBEAT_URL", "").strip()
    return raw or None


def infer_watch_type(url: str) -> str:
    """Infer watch type from URL path structure."""
    category_markers = [
        "/collections/", "/category/", "/shop/category/",
        "/series/", "/pre-orders/", "/trading-card-games/",
    ]
    if "/products/" in url:
        return "product"
    for marker in category_markers:
        if marker in url:
            return "category"
    return "product"
