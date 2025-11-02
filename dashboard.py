# dashboard.py
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

APP_TITLE = "CelestiGuard Dashboard"
VERSION = os.getenv("CELESTIGUARD_VERSION", "dev")

app = FastAPI(title=APP_TITLE)

# --- Templates & Static ---
templates_dir = Path("templates")
static_dir = Path("static")

# Mount static at /static if present; if not, try to mount templates for legacy assets.
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
elif templates_dir.is_dir():
    # For older setups where assets live in templates/
    app.mount("/static", StaticFiles(directory=str(templates_dir)), name="static")

templates = Jinja2Templates(directory=str(templates_dir)) if templates_dir.is_dir() else None


# --- Small helpers ---
def _find_changelog_path() -> Path | None:
    candidates = [
        Path("data/changelog.json"),
        Path("changelog.json"),
        Path("templates/changelog.json"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _no_store_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    }


# --- Health & Version ---
@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "version": VERSION})


@app.get("/api/version")
async def api_version():
    return JSONResponse({"version": VERSION})


# --- API endpoint for changelog ---
@app.get("/api/changelog")
async def api_changelog():
    """
    Always return a JSON LIST (possibly empty) and disable caching so the page
    never gets stuck on stale responses.
    """
    p = _find_changelog_path()
    items: List[Any] = []
    if p:
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            # Normalize to list
            if isinstance(data, dict):
                items = [data]
            elif isinstance(data, list):
                items = data
            else:
                items = []
        except Exception:
            items = []

    # Never 404; the page logic can show "No entries" nicely.
    return JSONResponse(items, headers=_no_store_headers())


# --- Webpage route ---
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """
    Renders index.html if templates/ exists, otherwise serves a tiny inline page
    that fetches /api/changelog and shows it.
    """
    if templates:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "title": APP_TITLE,
                "version": VERSION,
            },
        )

    # Fallback minimal HTML if Jinja templates aren't available
    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width,initial-scale=1" />
        <title>{APP_TITLE}</title>
        <style>
          body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 24px; }}
          .muted {{ color: #6b7280; }}
          .card {{ border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px; margin: 12px 0; }}
        </style>
      </head>
      <body>
        <h1>{APP_TITLE} <span class="muted">v{VERSION}</span></h1>
        <h2>Changelog</h2>
        <div id="cl">Loading…</div>

        <script>
          (async function() {{
            const el = document.getElementById('cl');
            try {{
              const res = await fetch('/api/changelog', {{ cache: 'no-store', headers: {{ 'Cache-Control': 'no-store' }} }});
              if (!res.ok) throw new Error('HTTP ' + res.status);
              const items = await res.json();
              if (!Array.isArray(items) || !items.length) {{
                el.textContent = 'No changelog entries yet.';
                return;
              }}
              el.innerHTML = items.map(entry => `
                <div class="card">
                  <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
                    <strong>${{entry.version || 'unversioned'}}</strong>
                    <span class="muted">${{entry.date || ''}}</span>
                  </div>
                  <ul style="margin:10px 0 0 18px">
                    ${(entry.changes || []).map(c => `<li>${{c}}</li>`).join('')}
                  </ul>
                </div>
              `).join('');
            }} catch (e) {{
              el.textContent = 'Failed to load changelog.';
            }}
          }})();
        </script>
      </body>
    </html>
    """
    return HTMLResponse(html)
