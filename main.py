import json
import os
import asyncio
import subprocess
import tkinter as tk
from pathlib import Path
from contextlib import asynccontextmanager
from html import escape

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
SCREEN_W: int = 1920
SCREEN_H: int = 1080


def detect_screen_size() -> tuple[int, int]:
    """Detect the primary monitor resolution at startup."""
    try:
        root = tk.Tk()
        root.withdraw()
        w = root.winfo_screenwidth()
        h = root.winfo_screenheight()
        root.destroy()
        return w, h
    except Exception:
        return 1920, 1080


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
                    f"--window-size={SCREEN_W},{SCREEN_H}",
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
# Template rendering (inline, no Jinja2 dependency)
# ---------------------------------------------------------------------------
def render_index(regions: list[dict]) -> str:
    """Render the index page — reads the HTML template and injects region accordion."""
    template = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")

    def _split_name(name: str) -> tuple[str, str]:
        """Split '互联网 (HLW)' into ('互联网', '（HLW）')."""
        if "(" in name:
            parts = name.rsplit("(", 1)
            title = parts[0].strip()
            subtitle = "（" + parts[1].rstrip(")").strip() + "）"
            return title, subtitle
        return name, ""

    def render_site_card(s: dict) -> str:
        sid = escape(s["id"], quote=True)
        stype = escape(s.get("type", "web"), quote=True)
        color_cls = " card-orange" if s.get("color") == "orange" else ""
        full_name = s.get("name", sid)
        title, subtitle = _split_name(full_name)
        sub_html = f'<div class="card-sub">{escape(subtitle)}</div>' if subtitle else ""

        return f"""\
        <div class="card{color_cls}" data-site-id="{sid}" data-type="{stype}">
          <div class="card-body">
            <div class="card-title">{escape(title)}</div>
            {sub_html}
          </div>
        </div>"""

    region_parts = []
    for r in regions:
        rid = escape(r["id"], quote=True)
        rname = escape(r.get("name", rid), quote=True)
        sites = r.get("sites", [])

        if sites:
            cards_html = "\n".join(render_site_card(s) for s in sites)
            grid = f"""<div class="grid">{cards_html}</div>"""
        else:
            grid = """<div class="region-empty">暂无已配置站点</div>"""

        region_parts.append(f"""\
    <div class="region" data-region-id="{rid}">
      <div class="region-header" onclick="toggleRegion('{rid}')">
        <h2>{rname}</h2>
        <span class="region-arrow" id="arrow-{rid}">&#9660;</span>
      </div>
      <div class="region-content" id="region-{rid}">
        {grid}
      </div>
    </div>""")

    regions_html = "\n".join(region_parts)
    if not regions:
        regions_html = """\
    <div class="empty-state">
      <p>&#x26A0;&#xFE0F; config.json 为空或未找到，请先配置网站列表。</p>
    </div>"""

    html = template.replace("<!-- REGIONS_PLACEHOLDER -->", regions_html)
    return html


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global SCREEN_W, SCREEN_H
    load_dotenv()
    SCREEN_W, SCREEN_H = detect_screen_size()
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
    regions = load_config()
    return HTMLResponse(render_index(regions))


@app.get("/api/config")
async def get_config():
    return JSONResponse(load_config())


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
            viewport={"width": SCREEN_W, "height": SCREEN_H},
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
