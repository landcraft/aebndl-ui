import os
import subprocess
import threading
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import logging

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aebndl-ui")

app = FastAPI()

# Mount Static and Templates
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Note: In docker, we might need to adjust paths, but for now assuming src structure
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Determine Source Path (for System Info)
# Assuming 'source' is at the root of the project, one level up from 'src'
PROJECT_ROOT = os.path.dirname(BASE_DIR)
SOURCE_DIR = os.path.join(PROJECT_ROOT, "source")

# Download Status Tracking (Simple in-memory for now)
download_status = {
    "status": "idle",
    "message": "Ready to download",
    "pid": None
}

def run_download(url: str, threads: int, resolution: str, scene: str, output_dir: str):
    global download_status
    download_status["status"] = "running"
    download_status["message"] = f"Starting download for {url}..."
    
    cmd = ["python3", "-m", "aebn_dl.cli", url]
    
    if threads:
        cmd.extend(["--threads", str(threads)])
    
    if resolution:
        # If resolution is provided, use it. If "0" (lowest), pass "0".
        cmd.extend(["--resolution", str(resolution)])
    
    if scene:
        # Determine if it's a range or single, user input is "Scene Index"
        # CLI supports -s for single scene. 
        cmd.extend(["--scene", str(scene)])
        
    if output_dir:
        cmd.extend(["--output_dir", output_dir])
        
    # Add non-interactive flags if possible or ensure it doesn't block.
    # The CLI seems designed for interaction? 
    # Based on help: "--force-resolution" might be good to avoid prompts.
    cmd.append("--force-resolution")

    logger.info(f"Running command: {' '.join(cmd)}")

    try:
        # We need to set PYTHONPATH to include the source directory so python -m aebn_dl.cli works
        env = os.environ.copy()
        env["PYTHONPATH"] = SOURCE_DIR + os.pathsep + env.get("PYTHONPATH", "")
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=PROJECT_ROOT # Run from project root
        )
        download_status["pid"] = process.pid
        
        # Capture output line by line
        for line in process.stdout:
            download_status["message"] = line.strip()
            logger.info(line.strip())
            
        process.wait()
        
        if process.returncode == 0:
            download_status["status"] = "completed"
            download_status["message"] = "Download finished successfully."
        else:
            download_status["status"] = "failed"
            download_status["message"] = f"Download failed with exit code {process.returncode}"
            
    except Exception as e:
        download_status["status"] = "error"
        download_status["message"] = f"Error: {str(e)}"
    finally:
        download_status["pid"] = None

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/download")
async def download(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    threads: int = Form(5),
    resolution: str = Form("0"), # Default to 0? Or let user pick.
    scene: str = Form(None),
):
    global download_status
    if download_status["status"] == "running":
        return JSONResponse(status_code=400, content={"error": "Download already in progress"})
    
    # Get Download Dir from Env or Default
    output_dir = os.environ.get("DOWNLOAD_DIR", "./downloads")
    
    background_tasks.add_task(run_download, url, threads, resolution, scene, output_dir)
    
    return {"message": "Download started", "status": "running"}

@app.get("/status")
async def get_status():
    return download_status

@app.get("/system-info")
async def system_info():
    # Attempt to read version from source/pyproject.toml or similar
    version = "Unknown"
    date = "Unknown"
    
    try:
        # Read Manifest (from our tracker) for date/SHA
        manifest_path = os.path.join(PROJECT_ROOT, "manifest.json")
        if os.path.exists(manifest_path):
            import json
            with open(manifest_path, 'r') as f:
                data = json.load(f)
                version = data.get("last_known_good_sha", "Unknown")[:7] # Short SHA
                date = data.get("last_update_timestamp", "Unknown")
    except Exception as e:
        logger.error(f"Error reading system info: {e}")
        
    return {"version": version, "date": date}
