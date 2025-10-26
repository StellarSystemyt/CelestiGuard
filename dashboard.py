<<<<<<< HEAD
# dashboard.py
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import json
import os

app = FastAPI(title="CelestiGuard Dashboard")

# serve templates and static files
app.mount("/static", StaticFiles(directory="templates"), name="static")
templates = Jinja2Templates(directory="templates")

# --- API endpoint for changelog ---
@app.get("/api/changelog")
async def get_changelog():
    changelog_path = "data/changelog.json"
    if not os.path.exists(changelog_path):
        return JSONResponse(content={"error": "Changelog not found."}, status_code=404)
    with open(changelog_path, "r") as f:
        data = json.load(f)
    return JSONResponse(content=data)

# --- Webpage route ---
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "title": "CelestiGuard Dashboard"})
=======
# dashboard.py
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import json
import os

app = FastAPI(title="CelestiGuard Dashboard")

# serve templates and static files
app.mount("/static", StaticFiles(directory="templates"), name="static")
templates = Jinja2Templates(directory="templates")

# --- API endpoint for changelog ---
@app.get("/api/changelog")
async def get_changelog():
    changelog_path = "data/changelog.json"
    if not os.path.exists(changelog_path):
        return JSONResponse(content={"error": "Changelog not found."}, status_code=404)
    with open(changelog_path, "r") as f:
        data = json.load(f)
    return JSONResponse(content=data)

# --- Webpage route ---
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "title": "CelestiGuard Dashboard"})
>>>>>>> dd54628 (start project)
