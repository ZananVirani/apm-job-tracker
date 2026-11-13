from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

from .models import Source


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Load a small, dependency-free subset of .env syntax."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def save_env_values(path: Path, values: dict[str, str], template_path: Path | None = None) -> None:
    """Update selected .env values atomically and restrict the file to the current user."""
    source = path if path.exists() else template_path
    lines = source.read_text(encoding="utf-8").splitlines() if source and source.exists() else []
    remaining = dict(values)
    updated: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                updated.append(f"{key}={remaining.pop(key)}")
                continue
        updated.append(line)
    if remaining:
        if updated and updated[-1]:
            updated.append("")
        updated.extend(f"{key}={value}" for key, value in remaining.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".env.",
        delete=False,
    ) as temporary:
        temporary.write("\n".join(updated).rstrip() + "\n")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return parsed


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return parsed


@dataclass(frozen=True)
class Settings:
    sources_path: Path
    state_path: Path
    interval_seconds: int
    request_timeout_seconds: int
    request_concurrency: int
    target_year: int
    graduation_year: int
    graduation_month: int
    alert_on_first_run: bool
    telegram_bot_token: str
    telegram_chat_id: str
    ntfy_topic: str
    ntfy_server: str
    additional_scan_minutes: int = 60
    max_description_fetches: int = 250
    description_refresh_hours: int = 24
    host_interval_seconds: float = 1.0
    close_after_hours: int = 24
    zero_result_warn_runs: int = 3
    export_dir: Path = PROJECT_ROOT / "data"

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        state_path = Path(os.getenv("STATE_PATH", str(PROJECT_ROOT / "data" / "notifier.sqlite3")))
        return cls(
            sources_path=Path(os.getenv("SOURCES_PATH", str(PROJECT_ROOT / "config" / "sources.json"))),
            state_path=state_path,
            interval_seconds=env_int("CHECK_INTERVAL_SECONDS", 300, 60),
            request_timeout_seconds=env_int("REQUEST_TIMEOUT_SECONDS", 25, 5),
            request_concurrency=env_int("REQUEST_CONCURRENCY", 8, 1),
            target_year=env_int("TARGET_YEAR", 2027, 2026),
            graduation_year=env_int("GRADUATION_YEAR", 2028, 2026),
            graduation_month=env_int("GRADUATION_MONTH", 4, 1),
            alert_on_first_run=env_bool("ALERT_ON_FIRST_RUN", True),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            ntfy_topic=os.getenv("NTFY_TOPIC", "").strip(),
            ntfy_server=os.getenv("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/"),
            additional_scan_minutes=env_int("ADDITIONAL_SCAN_MINUTES", 60, 0),
            max_description_fetches=env_int("MAX_DESCRIPTION_FETCHES", 250, 0),
            description_refresh_hours=env_int("DESCRIPTION_REFRESH_HOURS", 24, 1),
            host_interval_seconds=env_float("HOST_INTERVAL_SECONDS", 1.0, 0.0),
            close_after_hours=env_int("CLOSE_AFTER_HOURS", 24, 1),
            zero_result_warn_runs=env_int("ZERO_RESULT_WARN_RUNS", 3, 1),
            export_dir=Path(os.getenv("EXPORT_DIR", str(state_path.parent))),
        )


SOURCE_KEYS = {
    "id", "name", "urls", "career_url", "enabled", "headers", "url_template", "render", "request_method",
    "request_bodies", "verify_job_links", "paginate", "priority", "ats", "tier", "added_on",
    "discovered_via", "aggregator", "note",
}
TIERS = {"must", "additional"}


def load_sources(path: Path, include_disabled: bool = False) -> tuple[Source, ...]:
    from .adapters import ADAPTERS

    payload = json.loads(path.read_text(encoding="utf-8"))
    seen_ids: set[str] = set()
    sources: list[Source] = []
    for item in payload.get("sources", []):
        source_id = str(item["id"]).strip()
        if source_id in seen_ids:
            raise ValueError(f"Duplicate source id: {source_id}")
        seen_ids.add(source_id)
        ats = str(item.get("ats", "")).strip().casefold()
        if ats and ats not in ADAPTERS:
            raise ValueError(f"Source {source_id} uses unknown ats {ats!r}")
        urls = tuple(str(url).strip() for url in item.get("urls", []) if str(url).strip())
        if not urls and not ats:
            raise ValueError(f"Source {source_id} has no URLs")
        request_method = str(item.get("request_method", "GET")).strip().upper()
        if request_method not in {"GET", "POST"}:
            raise ValueError(f"Source {source_id} has unsupported request_method {request_method}")
        raw_bodies = item.get("request_bodies", [])
        request_bodies = tuple(
            value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
            for value in raw_bodies
        )
        if request_bodies and len(request_bodies) != len(urls):
            raise ValueError(f"Source {source_id} must have one request body per URL")
        tier = str(item.get("tier", "additional")).strip()
        if tier not in TIERS:
            raise ValueError(f"Source {source_id} has unsupported tier {tier!r}")
        ats_options = {key: value for key, value in item.items() if key not in SOURCE_KEYS}
        sources.append(
            Source(
                id=source_id,
                name=str(item["name"]).strip(),
                urls=urls,
                career_url=str(item.get("career_url") or (urls[0] if urls else "")).strip(),
                enabled=bool(item.get("enabled", True)),
                headers={str(key): str(value) for key, value in item.get("headers", {}).items()},
                url_template=str(item.get("url_template", "")).strip(),
                render=bool(item.get("render", False)),
                request_method=request_method,
                request_bodies=request_bodies,
                verify_job_links=bool(item.get("verify_job_links", False)),
                paginate=bool(item.get("paginate", False)),
                priority=bool(item.get("priority", False)),
                ats=ats,
                ats_options=ats_options,
                tier=tier,
                added_on=str(item.get("added_on", "")),
                discovered_via=str(item.get("discovered_via", "")),
                aggregator=bool(item.get("aggregator", False)),
            )
        )
    return tuple(source for source in sources if include_disabled or source.enabled)
