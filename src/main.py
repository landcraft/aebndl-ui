import os
import re
import shutil
import subprocess
import threading
import asyncio
import json
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
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
        self.lock = threading.RLock()
        self.shutdown_event = threading.Event()
        self.worker_threads = [] # Added for tracking worker threads
        
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
                        import signal
                        # Try graceful shutdown first (Ctrl+C simulation) which aebn_dl handles
                        job["process_obj"].send_signal(signal.SIGINT)
                        job["message"] = "Stopping..."
                        
                        # Wait a moment to see if it exits (optional, but good for cleanup)
                        # We won't block here long, let the worker loop handle the wait()
                        
                        # Mark as cancelled so the worker loop knows how to handle the exit code
                        # (worker loop sees process die -> checks this status)
                        job["status"] = "cancelled" 
                        return True
                    except Exception as e:
                        logger.error(f"Error terminating job {job_id}: {e}")
                        # Fallback to kill if needed (though wait loop usually handles this if we wanted to be robust)
                        try:
                            job["process_obj"].kill()
                        except:
                            pass
                
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
            # Look in history first (most likely place for retry), then active
            job = self.history.get(job_id) or self.active_jobs.get(job_id)
            
            if not job:
                return None
            
            # If it's already running (active + in queue or running), do nothing or return existing
            if job["status"] in ["queued", "running", "downloading", "muxing", "scraping"]:
                 return job_id

            # Reset status to queued
            job["status"] = "queued"
            job["message"] = "Restarting..."
            job["progress"] = 0
            job["pid"] = None
            job["process_obj"] = None
            
            # Move from history to active if needed
            if job_id in self.history:
                del self.history[job_id]
                self.active_jobs[job_id] = job
            
            # Add to queue
            if job_id not in self.queue:
                self.queue.append(job_id)
                
            return job_id

    def delete_job(self, job_id):
        with self.lock:
            # Check history first
            if job_id in self.history:
                del self.history[job_id]
                # Cleanup temp dir
                job_work_dir = os.path.join(PROJECT_ROOT, "temp", job_id)
                if os.path.exists(job_work_dir):
                    try:
                        shutil.rmtree(job_work_dir)
                    except Exception as e:
                        logger.error(f"Failed to cleanup temp dir {job_work_dir}: {e}")
                return True
            
            # If in active, only delete if not strictly "running" (queued is fine to cancel+delete)
            # But simpler: Cancel then Delete.
            
            if job_id in self.active_jobs:
                # If running, try to cancel first
                self.cancel_job(job_id)
                
                # Force remove from active status (UI expects "Remove" to remove)
                if job_id in self.active_jobs:
                    del self.active_jobs[job_id]
                
                # Cleanup temp dir (ONLY happens on manual delete now)
                job_work_dir = os.path.join(PROJECT_ROOT, "temp", job_id)
                if os.path.exists(job_work_dir):
                    try:
                        shutil.rmtree(job_work_dir)
                    except Exception as e:
                        logger.error(f"Failed to cleanup temp dir {job_work_dir}: {e}")

                return True
                
            return False

    def get_status(self):
        with self.lock:
            try:
                active_list = list(self.active_jobs.values())
                history_list = list(self.history.values())
                all_jobs = active_list + history_list
                # Return sanitized copies
                results = [self._sanitize_job(j) for j in all_jobs]
                
                return results
            except Exception as e:
                logger.error(f"Error in get_status: {e}")
                return []

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
        job_work_dir = None # Initialize outside try block for finally access
        try:
            with self.lock:
                if job_id in self.active_jobs:
                    job = self.active_jobs[job_id]
                    job["status"] = "running" # Initial running state
                    job["message"] = "Initializing..."
                    job["progress"] = 0
                    job["title"] = None
                    logger.info(f"[{job_id}] Processing. Active Jobs: {len(self.active_jobs)}/{self.max_concurrent}")
            
            if not job:
                return

            # Create isolated working directory
            job_work_dir = os.path.join(PROJECT_ROOT, "temp", job_id)
            os.makedirs(job_work_dir, exist_ok=True)
            
            cmd = ["python3", "-m", "aebn_dl.cli", job["url"]]
            if job["threads"]: cmd.extend(["--threads", str(job["threads"])])
            if job["resolution"]: cmd.extend(["--resolution", str(job["resolution"])])
            if job["scene"]: cmd.extend(["--scene", str(job["scene"])])
            if job["output_dir"]: cmd.extend(["--output_dir", job["output_dir"]])
            
            # Explicitly set work directory to isolated temp folder using short flag
            cmd.extend(["-w", job_work_dir])
            
            logger.info(f"[{job_id}] Running: {' '.join(cmd)}")
            
            env = os.environ.copy()
            env["PYTHONPATH"] = SOURCE_DIR + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONUNBUFFERED"] = "1"
            env["TERM"] = "xterm-256color" # Fake a real terminal
            env["FORCE_COLOR"] = "1" # Force Rich to render
            
            # Use PTY to force the subprocess to think it's in a real terminal
            # This ensures Rich prints the progress bars!
            import pty
            master_fd, slave_fd = pty.openpty()
            
            # Run the process in the ISOLATED working directory
            process = subprocess.Popen(
                cmd,
                stdout=slave_fd,
                stderr=slave_fd, # Merge stderr to stdout (PTY handles this)
                text=True,
                bufsize=1,
                universal_newlines=True,
                cwd=job_work_dir,  # Use isolated dir
                env=env,
                close_fds=True # Important for PTY
            )
            
            # Close slave fd in parent process (so we get EOF when child dies)
            os.close(slave_fd)
            
            with self.lock:
                job["pid"] = process.pid
                job["process_obj"] = process
            
            # Parsing State
            
            # Helper to strip ANSI escape codes
            def clean_ansi(text):
                ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
                return ansi_escape.sub('', text)

            # Regex patterns (Updated for Rich output)
            re_filename = re.compile(r"Output file name:\s*(.*)")
            re_scraping = re.compile(r"Scraping movie info")
            re_downloading = re.compile(r"Downloading segments")
            
            # Rich Progress: "Video download: 12/34 ... 45% ... 0:01:23 00:04:56"
            # Regex to find: "Video download:", then digits%, then TWO time-like strings
            # Group 1: Progress %, Group 2: Elapsed, Group 3: ETA
            re_audio = re.compile(r"Audio download:.*?(\d+)%.*?(\d{1,2}:\d{2}(?::\d{2})?).*?(\d{1,2}:\d{2}(?::\d{2})?)")
            re_video = re.compile(r"Video download:.*?(\d+)%.*?(\d{1,2}:\d{2}(?::\d{2})?).*?(\d{1,2}:\d{2}(?::\d{2})?)")
            
            # Fallback if time not found (e.g. start)
            re_audio_simple = re.compile(r"Audio download:.*?(\d+)%")
            re_video_simple = re.compile(r"Video download:.*?(\d+)%")
            
            re_merging = re.compile(r"Merging (?:video|audio) segments.*?(\d+)%")
            re_muxing = re.compile(r"Muxing streams")
            re_cleanup = re.compile(r"Deleted temp files")
            
            current_audio_prog = 0
            current_video_prog = 0
            
            # Helper to read stream char by char/chunk to handle \r
            def read_stream(master_fd):
                buffer = ""
                decoder =  threading.local() # Just in case, but simple decode works
                
                while True:
                    try:
                        # Read from PTY master
                        # Note: os.read returns bytes, need to decode
                        data = os.read(master_fd, 1024) 
                        if not data:
                            break
                        
                        chunk = data.decode('utf-8', errors='replace')
                        
                        # Process chunk
                        for char in chunk:
                             if char == '\n' or char == '\r':
                                 if buffer.strip():
                                     yield buffer
                                 buffer = ""
                             else:
                                 buffer += char
                                 
                    except OSError:
                        # Input/output error usually means the slave (process) closed
                        break
                
                # Flush remaining
                if buffer.strip():
                     yield buffer
            
            # Pass the master_fd to the reader
            for line in read_stream(master_fd):
                # Clean ANSI codes immediately
                clean_line = clean_ansi(line).strip()
                
                if not clean_line:
                    continue
                
                # Log raw for debug
                logger.info(f"[{job_id}] CLI: {clean_line}")
                
                # Skip status updates if we are cancelling
                # We need to lock to check safely, or just check the local dict ref (atomic in python roughly)
                # But safer to check inside specific update blocks or just ignore parsing if cancelled?
                # If we ignore parsing, we might miss "Cleaned up" message, but that's fine for cancellation.
                
                should_skip = False
                with self.lock:
                    if job.get("status") == "cancelled":
                        should_skip = True
                if should_skip:
                    continue

                # Phase 1: Setup / Scraping
                if re_scraping.search(clean_line):
                     with self.lock:
                        job["status"] = "scraping"
                        job["message"] = "Fetching Metadata..."

                # Phase 2: Start Downloading segments
                if re_downloading.search(clean_line):
                     with self.lock:
                        job["status"] = "downloading"
                        job["message"] = "Starting download..."

                # Parse Filename
                m_name = re_filename.search(clean_line)
                if m_name:
                    found_name = m_name.group(1).strip()
                    with self.lock:
                        job["title"] = found_name
                        # Extract parsed resolution
                        m_res = re.search(r"(\d{3,4}p)", found_name)
                        if m_res:
                            job["resolution"] = m_res.group(1)

                        if job["status"] == "scraping":
                             job["status"] = "downloading" 

                # Parse Progress (Download)
                # Video (Priority)
                m_video = re_video.search(clean_line)
                if m_video:
                    current_video_prog = int(m_video.group(1))
                    # Group 2 is Elapsed, Group 3 is ETA
                    eta = m_video.group(3)
                    with self.lock:
                        job["progress"] = current_video_prog
                        job["eta"] = eta
                        job["message"] = f"Downloading Video: {current_video_prog}% (ETA: {eta})"
                        
                elif re_video_simple.search(clean_line):
                    m = re_video_simple.search(clean_line)
                    current_video_prog = int(m.group(1))
                    with self.lock:
                        job["progress"] = current_video_prog
                        # Keep existing ETA if valid
                
                # Audio
                # We track it but main status is driven by video if both active
                m_audio = re_audio.search(clean_line)
                if m_audio:
                    current_audio_prog = int(m_audio.group(1))
                    # Optional: Could update message to say "V: 50% A: 40%"?
                    # For now keep simple
                    
                # Phase 3: Merging & Muxing
                m_merge = re_merging.search(clean_line)
                if m_merge:
                    with self.lock:
                        job["status"] = "muxing"
                        job["message"] = clean_line
                        job["progress"] = int(m_merge.group(1))
                        job["eta"] = ""

                if re_muxing.search(clean_line):
                     with self.lock:
                        job["status"] = "muxing"
                        job["message"] = "Finalizing File (Muxing)..."
                        job["progress"] = 99
                        job["eta"] = ""
                
                if "Muxing success" in clean_line:
                    with self.lock:
                         job["progress"] = 99

                # Error Detection
                if "RuntimeError:" in clean_line or "Error:" in clean_line:
                    # Filter out "Error downloading segment" which are retried
                    if "downloading segment" not in clean_line.lower():
                        error_msg = clean_line.split(":", 1)[1].strip() if ":" in clean_line else clean_line
                        with self.lock:
                            job["message"] = f"Error: {error_msg}"
                            # Don't set status to error yet, wait for process exit

                # Phase 4: Cleanup
                if re_cleanup.search(clean_line):
                    with self.lock:
                        job["status"] = "cleaning"
                        job["message"] = "Cleaning up..."
                        job["eta"] = ""

            process.wait()
            
            with self.lock:
                if process.returncode == 0:
                    if job["progress"] == 0:
                         job["status"] = "failed"
                         job["message"] = f"Finished with 0% progress. Check logs."
                         logger.warning(f"Job {job_id} finished with 0% progress. CLI output possibly not parsed.")
                    else:
                        job["status"] = "completed"
                        job["progress"] = 100
                        job["message"] = "Download finished."
                        job["eta"] = ""
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
            if job["status"] == "completed":
                import time
                time.sleep(5) # Give user a moment to see "Completed" status
                with self.lock:
                    if job_id in self.active_jobs:
                        # Success: Remove entirely (do not save to history)
                        self.active_jobs.pop(job_id)
            else:
                # Failed/Error/Cancelled: Move to history so user can retry/inspect
                with self.lock:
                    if job_id in self.active_jobs:
                         self.history[job_id] = self.active_jobs.pop(job_id)
                with self.lock:
                    if job_id in self.active_jobs:
                         self.history[job_id] = self.active_jobs.pop(job_id)
            
            # Cleanup temp dir
            # ONLY cleanup if completed (success). 
            # If failed/cancelled, keep dir so user can restart/resume.
            # Manual delete_job handles explicit cleanup.
            if job["status"] == "completed":
                if job_work_dir and os.path.exists(job_work_dir):
                    try:
                        shutil.rmtree(job_work_dir)
                    except Exception as e:
                        logger.error(f"Failed to cleanup temp dir {job_work_dir}: {e}")


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

@app.get("/stream-status")
async def stream_status(request: Request):
    """Server-Sent Events (SSE) for real-time status updates."""
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            
            # GetData
            data = manager.get_status()
            yield f"data: {json.dumps(data)}\n\n"
            
            # Wait for 1 second (polled server-side is better than reconnecting http)
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

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
