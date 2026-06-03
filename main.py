import json
import os
import asyncio
import subprocess
import sys
from pathlib import Path
from contextlib import asynccontextmanager

# ── Point Playwright to system-installed browsers ──────────────
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    os.path.expanduser("~/AppData/Local/ms-playwright"),
)

import dns.resolver
import dns.exception

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# ---------------------------------------------------------------------------
# Globals — a single headed browser instance shared across requests
# NOTE: Playwright runs via sync_api wrapped in run_in_executor to work
# around Python 3.13 Windows asyncio subprocess issue (NotImplementedError)
# ---------------------------------------------------------------------------
_playwright = None
_browser = None
_browser_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_config() -> list[dict]:
    if not CONFIG_PATH.exists():
        return []
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_site(site_id: str, regions: list[dict]) -> dict | None:
    """Search across all regions for a site by its id."""
    for region in regions:
        for site in region.get("sites", []):
            if site["id"] == site_id:
                return site
    return None


def get_credentials(site_id: str) -> tuple[str | None, str | None]:
    prefix = site_id.upper()
    username = os.getenv(f"{prefix}_USER")
    password = os.getenv(f"{prefix}_PASSWORD")
    return username, password


# ── Synchronous Playwright helpers (run in executor) ──────────────

def _ensure_browser_sync():
    """Get or create the shared browser instance (sync, runs in executor)."""
    global _playwright, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    # Close stale browser if any
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright is None:
        _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(
        headless=False,
        args=[
            "--start-maximized",
            "--ignore-certificate-errors",
            "--disable-web-security",
        ],
    )
    return _browser


def _shutdown_browser_sync():
    """Close browser and stop playwright (sync)."""
    global _playwright, _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright is not None:
        try:
            _playwright.stop()
        except Exception:
            pass
        _playwright = None


def _do_login_sync(site: dict, username: str | None, password: str | None) -> dict:
    """Synchronous login flow — runs in a thread via asyncio.to_thread."""
    print(f"[AuthDash] _do_login_sync starting for {site.get('id', '?')}", flush=True)
    browser = _ensure_browser_sync()
    print(f"[AuthDash] browser ready, creating context", flush=True)
    context = browser.new_context(
        no_viewport=True,
        ignore_https_errors=True,
    )
    page = context.new_page()

    selectors = site.get("selectors", {})

    # Auto-dismiss dialogs
    page.on("dialog", lambda dialog: dialog.accept())

    page.goto(site["url"], wait_until="load", timeout=30000)

    # Click pre-fill elements (e.g. switching to SMS/login tab)
    for tab_sel in selectors.get("pre_fill_clicks", []):
        try:
            page.wait_for_timeout(2000)
            el = page.query_selector(tab_sel)
            if el:
                el.click()
            page.wait_for_timeout(2000)
        except Exception:
            pass

    if selectors.get("username") and username:
        try:
            page.wait_for_selector(selectors["username"], state="attached", timeout=15000)
            page.fill(selectors["username"], username)
        except Exception:
            page.evaluate("([sel, val]) => { const el = document.querySelector(sel); if (el) el.value = val; }",
                          [selectors["username"], username])
            page.fill(selectors["username"], username)

    if selectors.get("password") and password:
        page.wait_for_selector(selectors["password"], timeout=10000)
        page.fill(selectors["password"], password)

    # Pre-checks (checkboxes / agreements)
    for check_selector in selectors.get("pre_checks", []):
        try:
            page.wait_for_selector(check_selector, timeout=3000)
            page.click(check_selector, force=True)
        except Exception:
            pass

    # Click login button
    btn = selectors.get("login_button")
    if btn:
        try:
            page.wait_for_selector(btn, timeout=5000)
            page.click(btn, force=True)
        except Exception:
            pass

    return {
        "status": "ok",
        "message": f"已打开 {site['name']} 并填入凭据。浏览器已就绪，请手动操作。",
    }


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------
def render_index() -> str:
    return (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    yield
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _shutdown_browser_sync)


app = FastAPI(title="AuthDash", version="0.1.0", lifespan=lifespan)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(render_index())


@app.get("/api/config")
async def get_config():
    return JSONResponse(load_config())


# ---------------------------------------------------------------------------
# DNS Query
# ---------------------------------------------------------------------------
RECORD_TYPES_MAP = {
    "A": lambda r: str(r.address),
    "AAAA": lambda r: str(r.address),
    "CNAME": lambda r: str(r.target),
    "MX": lambda r: f"{r.preference} {r.exchange}",
    "NS": lambda r: str(r.target),
    "TXT": lambda r: " ".join(r.strings) if r.strings else "",
}


@app.post("/api/dns/query")
async def dns_query(data: dict):
    domain = data.get("domain", "").strip()

    if not domain:
        raise HTTPException(status_code=400, detail="请输入域名")

    loop = asyncio.get_event_loop()

    try:
        all_records = []
        for rt in ("A", "AAAA", "CNAME", "MX", "NS", "TXT"):
            try:
                answers = await loop.run_in_executor(
                    None, lambda rt=rt: dns.resolver.resolve(domain, rt, raise_on_no_answer=False)
                )
                fmt = RECORD_TYPES_MAP.get(rt, str)
                for r in answers:
                    ttl = answers.rrset.ttl if answers.rrset else None
                    all_records.append({"value": fmt(r), "ttl": ttl, "rtype": rt})
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                pass
            except Exception:
                pass

        if not all_records:
            try:
                dns.resolver.resolve(domain, "A")
            except dns.resolver.NXDOMAIN:
                raise HTTPException(status_code=404, detail=f"域名 '{domain}' 不存在")

        return JSONResponse({"domain": domain, "records": all_records})

    except dns.exception.Timeout:
        raise HTTPException(status_code=504, detail="DNS 查询超时")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/login/{site_id}")
async def login(site_id: str):
    regions = load_config()
    site = find_site(site_id, regions)
    if site is None:
        raise HTTPException(status_code=404, detail=f"Site '{site_id}' not found in config.json")
    if site.get("type") == "app":
        raise HTTPException(status_code=400, detail=f"Site '{site_id}' is an app-type item, use /api/app-launch/")

    selectors = site.get("selectors", {})
    username, password = get_credentials(site_id)

    if selectors.get("username") and not username:
        raise HTTPException(
            status_code=400,
            detail=f"Missing username for {site_id} in .env (expected {site_id.upper()}_USER)",
        )
    if selectors.get("password") and not password:
        raise HTTPException(
            status_code=400,
            detail=f"Missing password for {site_id} in .env (expected {site_id.upper()}_PASSWORD)",
        )

    try:
        # Use asyncio.wait_for to enforce an overall deadline
        result = await asyncio.wait_for(
            asyncio.to_thread(_do_login_sync, site, username, password),
            timeout=60,
        )
        return JSONResponse(result)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="登录操作超时（60 秒），浏览器可能卡在启动或页面加载")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        detail = f"{type(e).__name__}: {e}"
        print(f"[AuthDash] login error: {detail}", flush=True)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=detail)


# ---------------------------------------------------------------------------
# App-launch endpoint (for type="app" sites)
# ---------------------------------------------------------------------------
@app.post("/api/app-launch/{site_id}")
async def app_launch(site_id: str):
    regions = load_config()
    site = find_site(site_id, regions)
    if site is None:
        raise HTTPException(status_code=404, detail=f"Site '{site_id}' not found in config.json")
    if site.get("type") != "app":
        raise HTTPException(status_code=400, detail=f"Site '{site_id}' is not an app-type item")

    app_path = site.get("app_path", "")
    if not app_path or not os.path.exists(app_path):
        raise HTTPException(status_code=400, detail=f"App not found: {app_path}")

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _launch_app_and_fill, site)
        return JSONResponse({
            "status": "ok",
            "message": f"已启动 {site['name']}。",
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _launch_app_and_fill(site: dict):
    """Launch a desktop app and bring its window to the foreground."""
    import psutil
    from pywinauto import Desktop

    app_path = site["app_path"]

    # 1. Check if yuanbao.exe is already running (Tgent's actual process)
    yuanbao_proc = None
    for p in psutil.process_iter(['pid', 'name']):
        try:
            if p.info['name'] and 'yuanbao' in p.info['name'].lower():
                yuanbao_proc = p
                break
        except Exception:
            pass

    if yuanbao_proc:
        # Already running — try to bring its window to front
        try:
            windows = Desktop(backend="uia").windows()
            for w in windows:
                title = w.window_text()
                if title and ("yuanbao" in title.lower() or "天翼" in title):
                    try:
                        w.set_focus()
                    except Exception:
                        pass
                    return
        except Exception:
            pass

    # 2. Not running or window not found — launch fresh
    os.startfile(app_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("AUTHDASH_PORT", "8000"))
    uvicorn.run("main:app", host="127.0.0.1", port=port, reload=False)
