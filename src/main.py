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

# Download Status Tracking
# Dictionary key = unique ID (UUID)
active_downloads = {}
download_lock = threading.Lock()

def run_download(job_id: str, url: str, threads: int, resolution: str, scene: str, output_dir: str):
    global active_downloads
    
    with download_lock:
        active_downloads[job_id]["status"] = "running"
        active_downloads[job_id]["message"] = f"Starting download for {url}..."
    
    cmd = ["python3", "-m", "aebn_dl.cli", url]
    
    if threads:
        cmd.extend(["--threads", str(threads)])
    
    if resolution:
        cmd.extend(["--resolution", str(resolution)])
    
    if scene:
        cmd.extend(["--scene", str(scene)])
        
    if output_dir:
        cmd.extend(["--output_dir", output_dir])
        
    cmd.append("--force-resolution")

    logger.info(f"Running command: {' '.join(cmd)}")

    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = SOURCE_DIR + os.pathsep + env.get("PYTHONPATH", "")
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # Merge stderr to stdout for simplicity
            text=True,
            env=env,
            cwd=PROJECT_ROOT
        )
        
        with download_lock:
             active_downloads[job_id]["pid"] = process.pid
        
        # Capture output line by line
        for line in process.stdout:
            line = line.strip()
            if line:
                with download_lock:
                    active_downloads[job_id]["message"] = line
                logger.info(f"[{job_id}] {line}")
            
        process.wait()
        
        with download_lock:
            if process.returncode == 0:
                active_downloads[job_id]["status"] = "completed"
                active_downloads[job_id]["message"] = "Download finished successfully."
            else:
                active_downloads[job_id]["status"] = "failed"
                active_downloads[job_id]["message"] = f"Download failed with exit code {process.returncode}"
            
    except Exception as e:
        with download_lock:
            active_downloads[job_id]["status"] = "error"
            active_downloads[job_id]["message"] = f"Error: {str(e)}"
    finally:
        with download_lock:
            active_downloads[job_id]["pid"] = None

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/download")
async def download(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    threads: int = Form(5),
    resolution: str = Form("720"), 
    scene: str = Form(None),
):
    global active_downloads
    
    # Generate a simple ID
    import uuid
    job_id = str(uuid.uuid4())[:8]
    
    # Cleanup finished jobs if list gets too long? 
    # For now, let's keep it simple. User sees all history until restart.
    
    with download_lock:
        active_downloads[job_id] = {
            "id": job_id,
            "url": url,
            "status": "pending",
            "message": "Queued...",
            "pid": None
        }
    
    # Get Download Dir from Env or Default
    output_dir = os.environ.get("DOWNLOAD_DIR", "./downloads")
    
    background_tasks.add_task(run_download, job_id, url, threads, resolution, scene, output_dir)
    
    return {"message": "Download started", "job_id": job_id, "status": "pending"}

@app.get("/status")
async def get_status():
    with download_lock:
        # Return list of downloads
        return list(active_downloads.values())

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
