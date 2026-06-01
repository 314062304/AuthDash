import json
import os
import asyncio
import subprocess
from pathlib import Path
from contextlib import asynccontextmanager

import dns.resolver
import dns.exception

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, Browser, Playwright
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
# ---------------------------------------------------------------------------
_playwright: Playwright | None = None
_browser: Browser | None = None
_lock = asyncio.Lock()


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


async def ensure_browser() -> Browser:
    global _playwright, _browser
    async with _lock:
        if _browser is not None and not _browser.is_connected():
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None
        if _browser is None:
            if _playwright is None:
                _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                headless=False,
                args=[
                    "--start-maximized",
                    "--ignore-certificate-errors",
                    "--disable-web-security",
                ],
            )
        return _browser


async def shutdown_browser():
    global _playwright, _browser
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None


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
    await shutdown_browser()


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
        browser = await ensure_browser()
        context = await browser.new_context(
            no_viewport=True,
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # Auto-dismiss permission / certificate dialogs
        page.on("dialog", lambda dialog: dialog.accept())

        await page.goto(site["url"], wait_until="load", timeout=30000)

        # Click pre-fill elements (e.g. switching to SMS/login tab) before filling
        for tab_sel in selectors.get("pre_fill_clicks", []):
            try:
                await page.wait_for_timeout(2000)
                await page.evaluate("(sel) => { const el = document.querySelector(sel); if(el) el.click(); }", tab_sel)
                await page.wait_for_timeout(2000)
            except Exception:
                pass

        if selectors.get("username"):
            try:
                # Use 'attached' instead of 'visible' — the input may be rendered but behind CSS transitions
                await page.wait_for_selector(selectors["username"], state="attached", timeout=15000)
                await page.fill(selectors["username"], username)
            except Exception:
                await page.evaluate(f"""([sel, val]) => {{
                    const el = document.querySelector(sel);
                    if (el) el.value = val;
                }})""", [selectors["username"], username])
            await page.fill(selectors["username"], username)

        if selectors.get("password"):
            await page.wait_for_selector(selectors["password"], timeout=10000)
            await page.fill(selectors["password"], password)

        # Click pre-checks (checkboxes / agreements) before login button
        for check_selector in selectors.get("pre_checks", []):
            try:
                await page.wait_for_selector(check_selector, timeout=3000)
                await page.click(check_selector, force=True)
            except Exception:
                pass

        # Click login button (best-effort)
        btn = selectors.get("login_button")
        if btn:
            try:
                await page.wait_for_selector(btn, timeout=5000)
                await page.click(btn, force=True)
            except Exception:
                pass

        return JSONResponse({
            "status": "ok",
            "message": f"Opened {site['name']} and filled credentials. Browser is ready for your manual input.",
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
