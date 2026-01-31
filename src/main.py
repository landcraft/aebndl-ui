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

# Download Manager
class DownloadManager:
    def __init__(self, max_concurrent=2):
        self.max_concurrent = max_concurrent
        self.queue = [] # List of job_ids waiting
        self.active_jobs = {} # ID -> Job Dict
        self.history = {} # ID -> Job Dict (completed/failed)
        self.lock = threading.Lock()
        self.shutdown_event = threading.Event()
        
        # Start worker threads
        self.workers = []
        for i in range(max_concurrent):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            self.workers.append(t)

    def add_job(self, url, threads, resolution, scene, output_dir):
        import uuid
        job_id = str(uuid.uuid4())[:8]
        
        job = {
            "id": job_id,
            "url": url,
            "threads": threads,
            "resolution": resolution,
            "scene": scene,
            "output_dir": output_dir,
            "status": "queued",
            "message": "Queued for download...",
            "pid": None,
            "process_obj": None, # To allow termination
            "start_time": None,
            "end_time": None
        }
        
        with self.lock:
            self.active_jobs[job_id] = job
            self.queue.append(job_id)
            
        return job_id

    def cancel_job(self, job_id):
        with self.lock:
            # Check if queued
            if job_id in self.queue:
                self.queue.remove(job_id)
                if job_id in self.active_jobs:
                    self.active_jobs[job_id]["status"] = "cancelled"
                    self.active_jobs[job_id]["message"] = "Cancelled by user."
                    # Move to history so it disappears from active view eventually? 
                    # Requirement: "removed from status window"
                    # We will treat cancelled like completed/failed for cleanup purposes.
                    self.history[job_id] = self.active_jobs.pop(job_id)
                return True
            
            # Check if running
            if job_id in self.active_jobs:
                job = self.active_jobs[job_id]
                if job["status"] == "running" and job["process_obj"]:
                    try:
                        job["process_obj"].terminate()
                        job["message"] = "Terminating process..."
                    except Exception as e:
                        logger.error(f"Error terminating job {job_id}: {e}")
                
                # We don't remove immediately, user might want to see "Cancelled"
                # But requirement says "remove from status window".
                # We'll mark it cancelled, the worker loop will see the exit and handle 'finished' logic.
                # Actually if we terminate, the worker loop `process.wait()` will return.
                return True
                
        return False

    def get_status(self):
        with self.lock:
            # Combine active and history for now, but filter?
            # Requirement: "Once a download is completed ... removed from status window ... unless it errors"
            # So return active_jobs + failed history. 
            # We also need completed/cancelled history for a short duration so the UI can show "Completed" before vanishing.
            
            all_jobs = list(self.active_jobs.values()) + list(self.history.values())
            return all_jobs

    def _worker_loop(self):
        while not self.shutdown_event.is_set():
            job_id = None
            
            with self.lock:
                if self.queue:
                    job_id = self.queue.pop(0)
            
            if not job_id:
                threading.Event().wait(1) # Sleep a bit
                continue
                
            # Process Job
            self._run_job(job_id)

    def _run_job(self, job_id):
        job = None
        with self.lock:
            if job_id in self.active_jobs:
                job = self.active_jobs[job_id]
                job["status"] = "running"
                job["message"] = f"Starting download..."
        
        if not job:
            return

        cmd = ["python3", "-m", "aebn_dl.cli", job["url"]]
        if job["threads"]: cmd.extend(["--threads", str(job["threads"])])
        if job["resolution"]: cmd.extend(["--resolution", str(job["resolution"])])
        if job["scene"]: cmd.extend(["--scene", str(job["scene"])])
        if job["output_dir"]: cmd.extend(["--output_dir", job["output_dir"]])
        cmd.append("--force-resolution")
        
        logger.info(f"[{job_id}] Running: {' '.join(cmd)}")
        
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = SOURCE_DIR + os.pathsep + env.get("PYTHONPATH", "")
            
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env=env, cwd=PROJECT_ROOT
            )
            
            with self.lock:
                job["pid"] = process.pid
                job["process_obj"] = process
            
            # Read output
            for line in process.stdout:
                line = line.strip()
                if line:
                    with self.lock:
                        job["message"] = line
            
            process.wait()
            
            with self.lock:
                if process.returncode == 0:
                    job["status"] = "completed"
                    job["message"] = "Download finished."
                else:
                    # Check if it was cancelled intentionally (signal)
                    if job["status"] != "cancelled": 
                        job["status"] = "failed"
                        job["message"] = f"Failed (Exit Code: {process.returncode})"
            
        except Exception as e:
            logger.error(f"Job {job_id} error: {e}")
            with self.lock:
                job["status"] = "error"
                job["message"] = f"Error: {str(e)}"
        finally:
            with self.lock:
                job["process_obj"] = None
                job["pid"] = None
                
            # Post-processing: Requirement "Once completed, removed... unless error"
            # We keep it in active_jobs for a few seconds so UI can show "Completed", then move to history.
            if job["status"] == "completed" or job["status"] == "cancelled":
                import time
                time.sleep(5) # Valid because we are in a worker thread
                with self.lock:
                    if job_id in self.active_jobs:
                        self.history[job_id] = self.active_jobs.pop(job_id)
                        # And remove from history immediately? Or keep in internal history but UI ignores?
                        # Using a separate "cleanup" might be better but this is simple.
                        # Actually we essentially want to "forget" it.
                        self.history.pop(job_id, None) 
            else:
                # Failed/Error: Keep in active_jobs (or move to history but keep serving it)
                # Let's move to history to keep active_jobs clean for "Limit 2" logic? 
                # Actually, concurrency limit is based on `workers` or `active_jobs`?
                # The _worker_loop pulls from queue. "Active" in specific sense means "Holding a thread".
                # Once _run_job returns, the thread is free. Use active_jobs for UI status.
                with self.lock:
                    if job_id in self.active_jobs:
                         self.history[job_id] = self.active_jobs.pop(job_id)


manager = DownloadManager(max_concurrent=2)

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/download")
async def download(
    background_tasks: BackgroundTasks, # Not used for logic, but keeps FastAPI happy/async
    url: str = Form(...),
    threads: int = Form(5),
    resolution: str = Form("720"), 
    scene: str = Form(None),
):
    # Get Download Dir from Env or Default
    output_dir = os.environ.get("DOWNLOAD_DIR", "./downloads")
    
    job_id = manager.add_job(url, threads, resolution, scene, output_dir)
    
    return {"message": "Download queued", "job_id": job_id, "status": "queued"}

@app.post("/cancel/{job_id}")
async def cancel(job_id: str):
    success = manager.cancel_job(job_id)
    return {"success": success}

@app.get("/status")
async def get_status():
    return manager.get_status()

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
