import os
import re
import subprocess
import threading
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
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

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    if os.path.exists(os.path.join(BASE_DIR, "static", "favicon.svg")):
        return FileResponse(os.path.join(BASE_DIR, "static", "favicon.svg"))
    return JSONResponse(content={}, status_code=404)

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
            "message": "Queued (Waiting for available download slot...)",
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

    def _sanitize_job(self, job):
        # Create a copy and remove non-serializable fields
        safe_job = job.copy()
        safe_job.pop("process_obj", None)
        return safe_job

    def restart_job(self, job_id):
        with self.lock:
            # Look in history first, then active (though restarting active is weird, maybe just copy params)
            old_job = self.history.get(job_id) or self.active_jobs.get(job_id)
            
            if not old_job:
                return None
            
    def restart_job(self, job_id):
        with self.lock:
            # Look in history first, then active
            old_job = self.history.get(job_id) or self.active_jobs.get(job_id)
            
            if not old_job:
                return None
            
            # Remove the old job (Requirement: "remove the old one and add the new job")
            if job_id in self.history:
                del self.history[job_id]
            elif job_id in self.active_jobs:
                # If it's active/running, we should technically stop it first if it's running
                # But "restart" usually implies it's done/failed.
                # If user restarts a running job, we'll cancel it first.
                # However, calling self.cancel_job here would require releasing lock or careful recursion.
                # Simpler: Just remove it from active_jobs if it's there. 
                # Note: If it had a process running, it becomes orphaned. 
                # Best practice: terminate if running.
                job = self.active_jobs[job_id]
                if job.get("process_obj"):
                    try:
                        job["process_obj"].terminate()
                    except:
                        pass
                del self.active_jobs[job_id]
                if job_id in self.queue:
                     self.queue.remove(job_id)
            
            # Create new job with same params
            new_job_id = self.add_job(
                old_job["url"],
                old_job["threads"],
                old_job["resolution"],
                old_job["scene"],
                old_job["output_dir"]
            )
            return new_job_id

    def delete_job(self, job_id):
        with self.lock:
            # Check history first
            if job_id in self.history:
                del self.history[job_id]
                return True
            
            # If in active, only delete if not running? 
            # Or cancel and delete?
            # Let's say we can only delete if it's not strictly "running" (queued is fine to cancel+delete)
            # But simpler: Cancel then Delete.
            
            if job_id in self.active_jobs:
                # If running, try to cancel first
                self.cancel_job(job_id)
                # It might take a moment to move to history.
                # But `cancel_job` returns True if it initiated cancel.
                # We can force remove from queue if it was queued.
                # If it was running, it moves to history eventually.
                # For UI responsiveness, let's just trigger cancel. User can click delete again if it persists?
                # Or we can just let `cancel_job` handle the cleanup.
                
                # Implementation Detail: `cancel_job` already moves queued jobs to history.
                # Running jobs stay active until process exits.
                # So `delete_job` should mainly be for History items.
                # If user tries to delete a running job, we should probably Cancel it first.
                
                # Let's just return False if it's active/running, telling user to "Stop" first?
                # Or auto-stop.
                
                # Requirement: "Delete a job"
                # Let's try to cancel it, then allow deletion from history.
                self.cancel_job(job_id)
                return True # It will be effectively "deleted" or "cancelling"
                
            return False

    def get_status(self):
        with self.lock:
            all_jobs = list(self.active_jobs.values()) + list(self.history.values())
            # Return sanitized copies
            results = [self._sanitize_job(j) for j in all_jobs]
            
            # Add debug info to a metadata field if we wanted, but for now let's just log it
            # Or we could return a dict with metadata, but that breaks frontend array expectation
            # We'll rely on server logs for now.
            logger.debug(f"Status check: {len(self.active_jobs)} active, {len(self.queue)} in queue")
            return results

    def _worker_loop(self):
        thread_name = threading.current_thread().name
        logger.info(f"Worker {thread_name} started")
        while not self.shutdown_event.is_set():
            job_id = None
            
            with self.lock:
                if self.queue:
                    job_id = self.queue.pop(0)
                    logger.info(f"Worker {thread_name} popped job {job_id} from queue. Queue length now: {len(self.queue)}")
            
            if not job_id:
                threading.Event().wait(1) # Sleep a bit
                continue
                
            # Process Job
            logger.info(f"Worker {thread_name} processing job {job_id}")
            self._run_job(job_id)
            logger.info(f"Worker {thread_name} finished job {job_id}")

    def _run_job(self, job_id):
        job = None
        with self.lock:
            if job_id in self.active_jobs:
                job = self.active_jobs[job_id]
                job["status"] = "running" # Initial running state
                job["message"] = f"Starting download..."
                job["progress"] = 0
                job["title"] = None
        
        if not job:
            return

        cmd = ["python3", "-m", "aebn_dl.cli", job["url"]]
        if job["threads"]: cmd.extend(["--threads", str(job["threads"])])
        if job["resolution"]: cmd.extend(["--resolution", str(job["resolution"])])
        if job["scene"]: cmd.extend(["--scene", str(job["scene"])])
        if job["output_dir"]: cmd.extend(["--output_dir", job["output_dir"]])
        
        logger.info(f"[{job_id}] Running: {' '.join(cmd)}")
        
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = SOURCE_DIR + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONUNBUFFERED"] = "1"
            
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env=env, cwd=PROJECT_ROOT, bufsize=1, universal_newlines=True
            )
            
            with self.lock:
                job["pid"] = process.pid
                job["process_obj"] = process
            
            # Parsing State
            
            # Regex patterns
            re_filename = re.compile(r"Output file name:\s*(.*)")
            re_scraping = re.compile(r"Scraping movie info")
            re_downloading = re.compile(r"Downloading segments")
            re_audio = re.compile(r"Audio download:.*?(\d+)%")
            re_video = re.compile(r"Video download:.*?(\d+)%")
            re_merging = re.compile(r"Merging (?:video|audio) segments.*?(\d+)%")
            re_muxing = re.compile(r"Muxing streams")
            re_cleanup = re.compile(r"Deleted temp files")
            
            current_audio_prog = 0
            current_video_prog = 0
            
            # Helper to read stream char by char/chunk to handle \r
            def read_stream(stream):
                buffer = ""
                while True:
                    # Read larger chunks for efficiency, but scan for \r
                    chunk = stream.read(1) # Read 1 char at a time to be safe with blocking
                    if not chunk:
                        if buffer:
                            yield buffer
                        break
                    
                    if chunk == '\n' or chunk == '\r':
                        if buffer.strip():
                            yield buffer
                        buffer = ""
                    else:
                        buffer += chunk
            
            for line in read_stream(process.stdout):
                line = line.strip()
                if not line:
                    continue
                
                # Log CLI output for debugging
                logger.info(f"[{job_id}] CLI: {line}")
                
                # Phase 1: Setup / Scraping
                if re_scraping.search(line):
                     with self.lock:
                        job["status"] = "scraping"
                        job["message"] = "Fetching Metadata..."

                # Phase 2: Start Downloading segments
                if re_downloading.search(line):
                     with self.lock:
                        job["status"] = "downloading"
                        job["message"] = "Starting download..."

                # Parse Filename (can happen anytime during setup)
                m_name = re_filename.search(line)
                if m_name:
                    found_name = m_name.group(1).strip()
                    with self.lock:
                        job["title"] = found_name
                        
                        # Extract actual resolution from filename (e.g., "Movie 720p.mp4")
                        m_res = re.search(r"(\d{3,4}p)", found_name)
                        if m_res:
                            job["resolution"] = m_res.group(1)

                        # If we found name, we are likely past scraping or close to it
                        if job["status"] == "scraping":
                             job["status"] = "downloading" 

                # Parse Progress (Download)
                m_audio = re_audio.search(line)
                if m_audio:
                    current_audio_prog = int(m_audio.group(1))
                
                m_video = re_video.search(line)
                if m_video:
                    current_video_prog = int(m_video.group(1))
                    
                # Phase 3: Merging & Muxing
                m_merge = re_merging.search(line)
                if m_merge:
                    with self.lock:
                        job["status"] = "muxing"
                        job["message"] = line # "Merging video segments... XX%"
                        job["progress"] = int(m_merge.group(1))

                if re_muxing.search(line):
                     with self.lock:
                        job["status"] = "muxing"
                        job["message"] = "Finalizing File (Muxing)..."
                        job["progress"] = 99
                
                if "Muxing success" in line:
                    with self.lock:
                         job["progress"] = 99

                # Error Detection
                if "RuntimeError:" in line or "Error:" in line:
                    error_msg = line.split(":", 1)[1].strip() if ":" in line else line
                    with self.lock:
                        job["message"] = f"Error: {error_msg}"

                # Phase 4: Cleanup
                if re_cleanup.search(line):
                    with self.lock:
                        job["status"] = "cleaning"
                        job["message"] = "Cleaning up..."

                # Determine Progress Update
                if job["status"] == "downloading":
                    # User requested to track only Video progress
                    with self.lock:
                        job["progress"] = current_video_prog
                        pass

            process.wait()
            
            with self.lock:
                if process.returncode == 0:
                    if job["progress"] == 0:
                         job["status"] = "failed"
                         job["message"] = f"Finished with 0% progress. Check logs."
                         logger.warning(f"Job {job_id} finished with 0% progress. CLI might have exited early.")
                    else:
                        job["status"] = "completed"
                        job["progress"] = 100
                        job["message"] = "Download finished."
                else:
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
                
            # Post-processing
            if job["status"] == "completed" or job["status"] == "cancelled":
                import time
                time.sleep(5) 
                with self.lock:
                    if job_id in self.active_jobs:
                        self.history[job_id] = self.active_jobs.pop(job_id)

            else:
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
    threads: int = Form(10),
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

@app.post("/restart/{job_id}")
async def restart(job_id: str):
    new_id = manager.restart_job(job_id)
    if new_id:
        return {"success": True, "new_job_id": new_id}
    return {"success": False, "message": "Job not found"}

@app.delete("/delete/{job_id}")
async def delete(job_id: str):
    success = manager.delete_job(job_id)
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
