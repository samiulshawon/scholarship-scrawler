"""Async Playwright crawler with stealth, polite throttling, and pause/resume.

The crawler walks the queue produced by ``build_queue`` and, for each URL:

1. Honours robots.txt (cached per host).
2. Throttles per-domain to >= 1 second between hits.
3. Opens the page with stealth + randomised viewport + rotating User-Agent.
4. Extracts listings via the per-host selectors in ``selectors.yaml``.
5. Hands every record to the ``Verifier`` and stores the result in SQLite.
6. After every page, checks ``SessionState.is_paused`` and exits cleanly.

The raw extraction is intentionally generic. Real production polish lives in
``selectors.yaml`` (per-host) and in per-site adapters that you can register
via ``ScraperEngine.register_site_adapter``.
"""
from __future__ import annotations

import asyncio
import random
import time
import urllib.parse as urlp
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from bs4 import BeautifulSoup

try:                                            # Optional dependency: makes tests easier
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    from playwright_stealth import stealth_async
except ImportError:                              # pragma: no cover
    async_playwright = None                      # type: ignore[assignment]
    stealth_async = None                         # type: ignore[assignment]

from db import Database
from session_manager import SessionState
from verifier import Verifier


# ----------------------------------------------------------------- queue model
@dataclass
class QueueItem:
    country: str
    phase: str          # university | government | external | joint
    url: str
    region: str         # Europe | Australia | New Zealand


def build_queue(config: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Flatten config.yaml seeds into the strict country/phase order.

    Returns a list of ``(country, phase, url)`` tuples. Phase order within a
    country is fixed to: university -> government -> external -> joint.
    """
    phases = ("university", "government", "external", "joint")
    queue: list[tuple[str, str, str]] = []
    seeds = config.get("seeds", {}) or {}

    for region_key, region_cfg in (config.get("targets") or {}).items():
        for country in region_cfg.get("countries", []):
            country_seeds = seeds.get(country, {}) or {}
            for phase in phases:
                urls = country_seeds.get(phase) or []
                for url in urls:
                    if url:
                        queue.append((country, phase, url))
    return queue


def region_for(country: str, config: dict[str, Any]) -> str:
    if country in (config.get("targets", {}).get("europe", {}).get("countries") or []):
        return "Europe"
    if country == "Australia":
        return "Australia"
    if country == "New Zealand":
        return "New Zealand"
    return "Unknown"


# ----------------------------------------------------------------- robots.txt
class RobotsCache:
    """Tiny in-memory robots.txt cache. Failures fall back to 'allow'."""

    def __init__(self, user_agent: str = "*") -> None:
        self.user_agent = user_agent
        self._cache: dict[str, robotparser.RobotFileParser] = {}

    def can_fetch(self, url: str) -> bool:
        host = urlp.urlsplit(url).netloc
        if not host:
            return True
        if host not in self._cache:
            rp = robotparser.RobotFileParser()
            robots_url = f"{urlp.urlsplit(url).scheme}://{host}/robots.txt"
            try:
                rp.set_url(robots_url)
                rp.read()
            except Exception:
                # Network/parse errors: be permissive; per-domain block counter
                # will still kick in if the site reacts hostilely.
                rp = robotparser.RobotFileParser()
                rp.parse([])
            self._cache[host] = rp
        try:
            return self._cache[host].can_fetch(self.user_agent, url)
        except Exception:
            return True


# ----------------------------------------------------------------- throttle
class DomainThrottle:
    """Enforces a minimum interval between requests to the same host."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def wait(self, url: str) -> None:
        host = urlp.urlsplit(url).netloc
        async with self._lock:
            last = self._last.get(host, 0.0)
            wait_for = self.min_interval - (time.monotonic() - last)
            self._last[host] = time.monotonic() + max(wait_for, 0.0)
        if wait_for > 0:
            await asyncio.sleep(wait_for)


# ------------------------------------------------------------------- adapters
SiteAdapter = Callable[[str, str, dict[str, Any]], Awaitable[list[dict[str, Any]]]]
"""Signature: (html, page_url, config) -> list[record]."""


def _parse_with_selectors(html: str, selectors: dict[str, Any], page_url: str) -> list[dict[str, Any]]:
    """Generic extractor used as a fallback when no site adapter is registered.

    Pulls listings via CSS selectors from ``selectors.yaml`` and constructs a
    minimal record. The verifier handles the heavy lifting of classification.
    """
    soup = BeautifulSoup(html, "lxml")
    listing_sel = selectors.get("listing", "article, li")
    name_sel = selectors.get("name", "h1, h2, h3, a")
    link_sel = selectors.get("link", "a")
    deadline_sel = selectors.get("deadline", "time, .deadline")
    desc_sel = selectors.get("description", "p")

    records: list[dict[str, Any]] = []
    for node in soup.select(listing_sel):
        name_el = node.select_one(name_sel)
        link_el = node.select_one(link_sel)
        if not (name_el and link_el):
            continue
        href = link_el.get("href") or ""
        if not href:
            continue
        application_url = urlp.urljoin(page_url, href)
        deadline_el = node.select_one(deadline_sel)
        desc_el = node.select_one(desc_sel)
        records.append({
            "name": name_el.get_text(strip=True),
            "application_url": application_url,
            "source_page": page_url,
            "application_deadline": (deadline_el.get_text(strip=True) if deadline_el else ""),
            "description": (desc_el.get_text(" ", strip=True) if desc_el else node.get_text(" ", strip=True))[:4000],
            "raw_payload": node.get_text(" ", strip=True)[:4000],
        })
    return records


# ------------------------------------------------------------------- engine
@dataclass
class CrawlStats:
    pages_visited: int = 0
    records_found: int = 0
    countries: set[str] = field(default_factory=set)


class ScraperEngine:
    def __init__(
        self,
        config: dict[str, Any],
        selectors: dict[str, Any],
        db: Database,
        state: SessionState,
        verifier: Verifier,
        on_log: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.selectors = selectors
        self.db = db
        self.state = state
        self.verifier = verifier
        self.on_log = on_log or (lambda level, msg: None)

        scfg = config.get("scraper", {})
        self.min_delay = float(scfg.get("min_delay_seconds", 3.0))
        self.max_delay = float(scfg.get("max_delay_seconds", 7.0))
        self.retry_statuses = set(scfg.get("retry_on_status", [429, 503, 504]))
        self.max_retries = int(scfg.get("max_retries", 3))
        self.backoff_mult = float(scfg.get("backoff_multiplier", 2.0))
        self.headless = bool(scfg.get("headless", True))
        self.respect_robots = bool(scfg.get("respect_robots_txt", True))
        self.block_threshold = int(scfg.get("domain_block_threshold", 3))
        self.user_agents: list[str] = list(scfg.get("user_agents", []))
        self.viewport_min = tuple(scfg.get("viewport_min", [1280, 800]))
        self.viewport_max = tuple(scfg.get("viewport_max", [1920, 1080]))
        self.proxies = self._load_proxies(scfg.get("proxy_file"))

        self.throttle = DomainThrottle(float(scfg.get("per_domain_min_interval", 1.0)))
        self.robots = RobotsCache()
        self.adapters: dict[str, SiteAdapter] = {}
        self.stats = CrawlStats()

    # ------------------------------------------------------- public hooks
    def register_site_adapter(self, hostname: str, adapter: SiteAdapter) -> None:
        """Plug a bespoke parser for a host. The generic parser is used otherwise."""
        self.adapters[hostname] = adapter

    # ---------------------------------------------------------- internals
    def _log(self, level: str, msg: str) -> None:
        self.on_log(level, msg)
        self.state.log(level, msg)

    @staticmethod
    def _load_proxies(path: str | None) -> list[str]:
        if not path:
            return []
        p = Path(path)
        if not p.exists():
            return []
        return [
            line.strip() for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def _selectors_for(self, url: str) -> dict[str, Any]:
        host = urlp.urlsplit(url).netloc
        return self.selectors.get(host) or self.selectors.get("default", {})

    def _random_viewport(self) -> dict[str, int]:
        w = random.randint(self.viewport_min[0], self.viewport_max[0])
        h = random.randint(self.viewport_min[1], self.viewport_max[1])
        return {"width": w, "height": h}

    def _pick_proxy(self) -> dict[str, str] | None:
        if not self.proxies:
            return None
        return {"server": random.choice(self.proxies)}

    async def _human_delay(self) -> None:
        await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))

    async def _simulate_mouse(self, page: "Page") -> None:
        try:
            for _ in range(random.randint(2, 4)):
                x = random.randint(50, 1100)
                y = random.randint(50, 700)
                await page.mouse.move(x, y, steps=random.randint(5, 15))
                await asyncio.sleep(random.uniform(0.1, 0.4))
        except Exception:                            # mouse jitter is best-effort
            pass

    async def _new_context(self, browser: "Browser") -> "BrowserContext":
        ua = random.choice(self.user_agents) if self.user_agents else None
        ctx = await browser.new_context(
            user_agent=ua,
            viewport=self._random_viewport(),
            locale="en-US",
            ignore_https_errors=True,
        )
        return ctx

    # -------------------------------------------------------- core fetch
    async def _fetch(self, page: "Page", url: str) -> tuple[str | None, int | None]:
        """Fetch a URL with retry/backoff. Returns (html, status_code)."""
        delay = self.min_delay
        for attempt in range(1, self.max_retries + 1):
            await self.throttle.wait(url)
            try:
                resp = await page.goto(url, wait_until="networkidle", timeout=45_000)
            except Exception as exc:                 # timeout or navigation error
                self._log("error", f"navigation error on {url}: {exc}")
                if attempt == self.max_retries:
                    return None, None
                await asyncio.sleep(delay)
                delay *= self.backoff_mult
                continue

            status = resp.status if resp else None
            if status and status in self.retry_statuses:
                self._log("warn", f"status {status} on {url} (attempt {attempt})")
                host = urlp.urlsplit(url).netloc
                count = self.db.record_block(host, datetime.utcnow().isoformat(timespec="seconds"))
                if count >= self.block_threshold:
                    self.db.abandon_domain(host)
                    self._log("error", f"abandoning domain {host} after {count} blocks")
                    return None, status
                await asyncio.sleep(delay)
                delay *= self.backoff_mult
                continue

            try:
                html = await page.content()
            except Exception as exc:
                self._log("error", f"failed reading content: {exc}")
                return None, status
            return html, status
        return None, None

    # --------------------------------------------------------- parsing
    async def _extract(self, html: str, url: str) -> list[dict[str, Any]]:
        host = urlp.urlsplit(url).netloc
        adapter = self.adapters.get(host)
        if adapter:
            try:
                return await adapter(html, url, self.config)
            except Exception as exc:
                self._log("error", f"adapter for {host} failed: {exc}; falling back")
        return _parse_with_selectors(html, self._selectors_for(url), url)

    # -------------------------------------------------------- main loop
    async def run(self) -> None:
        if async_playwright is None:
            raise RuntimeError(
                "Playwright is not installed. Run `pip install -r requirements.txt` "
                "and `playwright install chromium`."
            )

        snapshot = self.state.snapshot()
        queue = [tuple(item) for item in snapshot.get("queue", [])]
        cursor = int(snapshot.get("cursor", 0))

        if not queue:
            self._log("warn", "queue is empty — nothing to crawl")
            return

        self._log("info", f"starting session {self.state.session_id} with {len(queue) - cursor} URLs")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                proxy=self._pick_proxy(),
            )
            try:
                context = await self._new_context(browser)
                page = await context.new_page()
                if stealth_async is not None:
                    await stealth_async(page)

                for idx in range(cursor, len(queue)):
                    if self.state.is_paused():
                        self._log("warn", "pause requested — exiting after current loop")
                        break

                    country, phase, url = queue[idx]
                    self.state.set_position(country, phase)
                    self.stats.countries.add(country)

                    if self.db.is_visited(url):
                        self._log("info", f"skip already-visited {url}")
                        self.state.advance(url)
                        continue

                    host = urlp.urlsplit(url).netloc
                    if self.db.is_abandoned(host):
                        self._log("warn", f"skip abandoned domain {host}")
                        self.state.advance(url)
                        continue

                    if self.respect_robots and not self.robots.can_fetch(url):
                        self._log("warn", f"robots.txt disallows {url}")
                        self.state.advance(url)
                        continue

                    self._log("info", f"[{country}/{phase}] fetching {url}")
                    html, status = await self._fetch(page, url)
                    self.db.mark_visited(
                        url=url,
                        session_id=self.state.session_id or "unknown",
                        country=country,
                        phase=phase,
                        visited_at=datetime.utcnow().isoformat(timespec="seconds"),
                        http_status=status,
                    )
                    self.state.advance(url)
                    self.stats.pages_visited += 1

                    if not html:
                        self._log("error", f"no html for {url}")
                        await self._human_delay()
                        continue

                    await self._simulate_mouse(page)

                    records = await self._extract(html, url)
                    region = region_for(country, self.config)
                    saved = 0
                    for record in records:
                        record.setdefault("country", country)
                        record.setdefault("region", region)
                        record.setdefault("provider", host)
                        record["session_id"] = self.state.session_id
                        result = self.verifier.verify(record)
                        if result.status == "rejected":
                            self._log("error", f"reject: {record.get('name','?')} ({result.score})")
                            continue
                        merged = result.merge_into(record)
                        self.db.upsert_scholarship(merged)
                        saved += 1
                        if result.status == "confirmed":
                            self._log("info", f"confirm: {merged.get('name','?')} ({result.score})")
                        else:
                            self._log("warn", f"probable: {merged.get('name','?')} ({result.score})")

                    if saved:
                        self.state.increment_records(saved)
                        self.stats.records_found += saved

                    await self._human_delay()
            finally:
                await browser.close()

        self._log("info", "crawl loop finished")


# ------------------------------------------------------------------ helpers
def load_yaml(path: str | Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


async def run_from_files(
    config_path: str | Path = "config.yaml",
    selectors_path: str | Path = "selectors.yaml",
    *,
    resume: bool = False,
    on_log: Callable[[str, str], None] | None = None,
) -> None:
    """Convenience entry point used by both CLI smoke tests and the UI."""
    config = load_yaml(config_path)
    selectors = load_yaml(selectors_path)
    db = Database(config["output"]["db_path"])
    state = SessionState(config["output"]["session_state_path"])
    verifier = Verifier(config)

    queue = build_queue(config)
    if resume and state.session_id:
        state.resume(queue)
        db.start_session(state.session_id, datetime.utcnow().isoformat(timespec="seconds"))
    else:
        from session_manager import make_session_id
        sid = make_session_id()
        db.start_session(sid, datetime.utcnow().isoformat(timespec="seconds"))
        state.init_session(sid, queue)

    engine = ScraperEngine(config, selectors, db, state, verifier, on_log=on_log)
    try:
        await engine.run()
    finally:
        snap = state.snapshot()
        db.end_session(
            session_id=snap.get("session_id", "unknown"),
            end_time=datetime.utcnow().isoformat(timespec="seconds"),
            countries=engine.stats.countries,
            pages=snap.get("pages_visited", 0),
            records=snap.get("records_found", 0),
            status="paused" if state.is_paused() else "completed",
        )


if __name__ == "__main__":                         # CLI smoke test
    asyncio.run(run_from_files())
