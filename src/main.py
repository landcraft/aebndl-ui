import os
import re
import shutil
import subprocess
import threading
import asyncio
import json
import uuid
import signal
import time
import logging
from collections import deque
from contextlib import asynccontextmanager
import secrets
from typing import Optional

# pyrefly: ignore [missing-import]
from fastapi import FastAPI, Request, Form, HTTPException, Depends, status
# pyrefly: ignore [missing-import]
from fastapi.security import HTTPBasic, HTTPBasicCredentials
# pyrefly: ignore [missing-import]
from fastapi.templating import Jinja2Templates
# pyrefly: ignore [missing-import]
from fastapi.staticfiles import StaticFiles
# pyrefly: ignore [missing-import]
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse

try:
    import pty
    HAS_PTY = True
except ImportError:
    pty = None
    HAS_PTY = False

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aebndl-ui")

# Optional HTTP Basic Authentication
AUTH_USERNAME = os.environ.get("AUTH_USERNAME")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD")
AUTH_ENABLED = bool(AUTH_USERNAME and AUTH_PASSWORD)
security = HTTPBasic(auto_error=False) if AUTH_ENABLED else None

async def verify_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security) if AUTH_ENABLED else None):
    if not AUTH_ENABLED:
        return True
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    is_user_ok = secrets.compare_digest(credentials.username, AUTH_USERNAME)
    is_pass_ok = secrets.compare_digest(credentials.password, AUTH_PASSWORD)
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
SOURCE_DIR = os.path.join(PROJECT_ROOT, "source")
TEMP_BASE = os.path.realpath(os.path.join(PROJECT_ROOT, "temp"))

# Download Manager
class DownloadManager:
    def __init__(self, max_concurrent=2):
        self.max_concurrent = max_concurrent
        self.queue = []  # List of job_ids waiting
        self.active_jobs = {}  # ID -> Job Dict
        self.history = {}  # ID -> Job Dict (completed/failed/cancelled)
        self.lock = threading.RLock()
        self.shutdown_event = threading.Event()
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Start worker threads
        self.workers = []
        for _ in range(max_concurrent):
            t = threading.Thread(target=self._worker_loop, daemon=True)
            t.start()
            self.workers.append(t)

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def add_subscriber(self, queue: asyncio.Queue):
        with self.lock:
            self._subscribers.add(queue)

    def remove_subscriber(self, queue: asyncio.Queue):
        with self.lock:
            self._subscribers.discard(queue)

    def notify_change(self):
        """Broadcast updated job status to all connected SSE clients."""
        if not self._loop or self._loop.is_closed():
            return
        status_data = self.get_status()
        with self.lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                self._loop.call_soon_threadsafe(self._safe_queue_put, q, status_data)
            except Exception:
                pass

    @staticmethod
    def _safe_queue_put(q: asyncio.Queue, data):
        try:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(data)
        except Exception:
            pass

    def add_job(self, url, threads, resolution, scene, output_dir, names=False, covers=False, split_scenes=False):
        job_id = str(uuid.uuid4())[:8]
        
        job = {
            "id": job_id,
            "url": url,
            "threads": threads,
            "resolution": resolution,
            "scene": scene,
            "names": names,
            "covers": covers,
            "split_scenes": split_scenes,
            "output_dir": output_dir,
            "status": "queued",
            "message": "Queued (Waiting for available download slot...)",
            "progress": 0,
            "eta": "",
            "title": None,
            "pid": None,
            "process_obj": None,
            "start_time": time.time(),
            "end_time": None,
            "completed_at": None,
            "logs": deque(maxlen=100),
        }
        
        with self.lock:
            self.active_jobs[job_id] = job
            self.queue.append(job_id)
            
        self.notify_change()
        return job_id

    def cancel_job(self, job_id):
        if not re.match(r"^[a-f0-9]{8}$", job_id):
            return False

        with self.lock:
            if job_id in self.queue:
                self.queue.remove(job_id)
                if job_id in self.active_jobs:
                    job = self.active_jobs.pop(job_id)
                    job["status"] = "cancelled"
                    job["message"] = "Cancelled by user."
                    self.history[job_id] = job
                self.notify_change()
                return True
            
            if job_id in self.active_jobs:
                job = self.active_jobs[job_id]
                if job["status"] in ["running", "downloading", "muxing", "scraping", "cleaning"] and job["process_obj"]:
                    try:
                        job["process_obj"].send_signal(signal.SIGINT)
                        job["message"] = "Stopping..."
                        job["status"] = "cancelled"
                        self.notify_change()
                        return True
                    except Exception as e:
                        logger.error(f"Error terminating job {job_id}: {e}")
                        try:
                            job["process_obj"].kill()
                        except Exception:
                            pass
                return True
                
        return False

    def restart_job(self, job_id):
        if not re.match(r"^[a-f0-9]{8}$", job_id):
            return None

        with self.lock:
            job = self.history.get(job_id) or self.active_jobs.get(job_id)
            if not job:
                return None
            
            if job["status"] in ["queued", "running", "downloading", "muxing", "scraping"]:
                return job_id

            job["status"] = "queued"
            job["message"] = "Restarting..."
            job["progress"] = 0
            job["eta"] = ""
            job["pid"] = None
            job["process_obj"] = None
            job["completed_at"] = None
            
            if job_id in self.history:
                del self.history[job_id]
                self.active_jobs[job_id] = job
            
            if job_id not in self.queue:
                self.queue.append(job_id)

        self.notify_change()
        return job_id

    def delete_job(self, job_id):
        if not re.match(r"^[a-f0-9]{8}$", job_id):
            return False

        with self.lock:
            job_found = False
            if job_id in self.history:
                del self.history[job_id]
                job_found = True
            elif job_id in self.active_jobs:
                self.cancel_job(job_id)
                if job_id in self.active_jobs:
                    del self.active_jobs[job_id]
                job_found = True

            # Safely cleanup temp workdir strictly within TEMP_BASE
            job_work_dir = os.path.realpath(os.path.join(TEMP_BASE, job_id))
            if job_work_dir.startswith(TEMP_BASE) and os.path.exists(job_work_dir):
                try:
                    shutil.rmtree(job_work_dir)
                except Exception as e:
                    logger.error(f"Failed to cleanup temp dir {job_work_dir}: {e}")

        if job_found:
            self.notify_change()
        return job_found

    def clear_completed_jobs(self):
        """Bulk-dismiss all completed downloads from history."""
        with self.lock:
            completed_ids = [
                jid for jid, j in self.history.items() if j.get("status") == "completed"
            ]
            for jid in completed_ids:
                del self.history[jid]
                work_dir = os.path.realpath(os.path.join(TEMP_BASE, jid))
                if work_dir.startswith(TEMP_BASE) and os.path.exists(work_dir):
                    try:
                        shutil.rmtree(work_dir)
                    except Exception:
                        pass
        self.notify_change()

    def get_job_logs(self, job_id):
        if not re.match(r"^[a-f0-9]{8}$", job_id):
            return None
        with self.lock:
            job = self.active_jobs.get(job_id) or self.history.get(job_id)
            if job and "logs" in job:
                return list(job["logs"])
        return None

    def _sanitize_job(self, job):
        safe_job = job.copy()
        safe_job.pop("process_obj", None)
        # Avoid bloating regular status updates with raw logs
        safe_job.pop("logs", None)
        return safe_job

    def get_status(self):
        with self.lock:
            try:
                active_list = list(self.active_jobs.values())
                history_list = list(self.history.values())
                all_jobs = active_list + history_list
                return [self._sanitize_job(j) for j in all_jobs]
            except Exception as e:
                logger.error(f"Error in get_status: {e}")
                return []

    def shutdown(self):
        self.shutdown_event.set()
        with self.lock:
            for _, job in list(self.active_jobs.items()):
                if job.get("process_obj"):
                    try:
                        job["process_obj"].terminate()
                    except Exception:
                        pass

    def _worker_loop(self):
        thread_name = threading.current_thread().name
        logger.info(f"Worker {thread_name} started")
        while not self.shutdown_event.is_set():
            job_id = None
            
            with self.lock:
                if self.queue:
                    job_id = self.queue.pop(0)
            
            if not job_id:
                threading.Event().wait(0.5)
                continue
                
            self._run_job(job_id)

    def _run_job(self, job_id):
        job = None
        job_work_dir = None
        master_fd = None
        slave_fd = None

        try:
            with self.lock:
                if job_id in self.active_jobs:
                    job = self.active_jobs[job_id]
                    job["status"] = "running"
                    job["message"] = "Initializing..."
                    job["progress"] = 0
                    job["title"] = None
                    logger.info(f"[{job_id}] Processing. Active Jobs: {len(self.active_jobs)}/{self.max_concurrent}")
            
            if not job:
                return

            self.notify_change()

            # Create isolated working directory
            job_work_dir = os.path.realpath(os.path.join(TEMP_BASE, job_id))
            os.makedirs(job_work_dir, exist_ok=True)
            
            # Construct command
            cmd = ["python3", "-m", "aebn_dl.cli", job["url"]]
            if job.get("threads"): cmd.extend(["--threads", str(job["threads"])])
            if job.get("resolution"): cmd.extend(["--resolution", str(job["resolution"])])
            if job.get("split_scenes"):
                cmd.append("--split-scenes")
            elif job.get("scene"):
                cmd.extend(["--scene", str(job["scene"])])
            if job.get("names"): cmd.append("--names")
            if job.get("covers"): cmd.append("--covers")
            if job.get("output_dir"): cmd.extend(["--output_dir", job["output_dir"]])
            
            cmd.extend(["-w", job_work_dir])
            logger.info(f"[{job_id}] Running: {' '.join(cmd)}")
            
            env = os.environ.copy()
            env["PYTHONPATH"] = SOURCE_DIR + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONUNBUFFERED"] = "1"
            env["TERM"] = "xterm-256color"
            env["FORCE_COLOR"] = "1"
            
            if HAS_PTY:
                master_fd, slave_fd = pty.openpty()
                stdout_target = slave_fd
                stderr_target = slave_fd
            else:
                stdout_target = subprocess.PIPE
                stderr_target = subprocess.STDOUT

            process = subprocess.Popen(
                cmd,
                stdout=stdout_target,
                stderr=stderr_target,
                text=True,
                bufsize=1,
                universal_newlines=True,
                cwd=job_work_dir,
                env=env,
                close_fds=True
            )
            
            if slave_fd is not None:
                os.close(slave_fd)
                slave_fd = None
            
            with self.lock:
                job["pid"] = process.pid
                job["process_obj"] = process
            
            # Parsing State & Regex
            def clean_ansi(text):
                ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
                return ansi_escape.sub('', text)

            re_filename = re.compile(r"Output file name:\s*(.*)")
            re_scraping = re.compile(r"Scraping movie info")
            re_downloading = re.compile(r"Downloading segments")
            re_audio = re.compile(r"Audio download:.*?(\d+)%.*?(\d{1,2}:\d{2}(?::\d{2})?).*?(\d{1,2}:\d{2}(?::\d{2})?)")
            re_video = re.compile(r"Video download:.*?(\d+)%.*?(\d{1,2}:\d{2}(?::\d{2})?).*?(\d{1,2}:\d{2}(?::\d{2})?)")
            re_video_simple = re.compile(r"Video download:.*?(\d+)%")
            re_merging = re.compile(r"Merging (?:video|audio) segments.*?(\d+)%")
            re_muxing = re.compile(r"Muxing streams")
            re_cleanup = re.compile(r"Deleted temp files")
            
            def read_stream(fd, proc):
                if fd is not None:
                    buffer = ""
                    while True:
                        try:
                            data = os.read(fd, 1024)
                            if not data:
                                break
                            chunk = data.decode('utf-8', errors='replace')
                            for char in chunk:
                                if char == '\n' or char == '\r':
                                    if buffer.strip():
                                        yield buffer
                                    buffer = ""
                                else:
                                    buffer += char
                        except OSError:
                            break
                    if buffer.strip():
                        yield buffer
                else:
                    if proc.stdout:
                        for l in iter(proc.stdout.readline, ''):
                            yield l

            last_notify_time = time.time()

            try:
                for line in read_stream(master_fd, process):
                    clean_line = clean_ansi(line).strip()
                    if not clean_line:
                        continue
                    
                    with self.lock:
                        job["logs"].append(clean_line)

                    # Check if cancelled
                    should_skip = False
                    with self.lock:
                        if job.get("status") == "cancelled":
                            should_skip = True
                    if should_skip:
                        continue

                    updated = False

                    if re_scraping.search(clean_line):
                        with self.lock:
                            job["status"] = "scraping"
                            job["message"] = "Fetching Metadata..."
                        updated = True

                    if re_downloading.search(clean_line):
                        with self.lock:
                            job["status"] = "downloading"
                            job["message"] = "Starting download..."
                        updated = True

                    m_name = re_filename.search(clean_line)
                    if m_name:
                        found_name = m_name.group(1).strip()
                        with self.lock:
                            job["title"] = found_name
                            m_res = re.search(r"(\d{3,4}p)", found_name)
                            if m_res:
                                job["resolution"] = m_res.group(1)
                            if job["status"] == "scraping":
                                job["status"] = "downloading"
                        updated = True

                    m_video = re_video.search(clean_line)
                    if m_video:
                        current_video_prog = int(m_video.group(1))
                        eta = m_video.group(3)
                        with self.lock:
                            job["progress"] = current_video_prog
                            job["eta"] = eta
                            job["message"] = f"Downloading Video: {current_video_prog}% (ETA: {eta})"
                        updated = True
                    elif re_video_simple.search(clean_line):
                        m = re_video_simple.search(clean_line)
                        current_video_prog = int(m.group(1))
                        with self.lock:
                            job["progress"] = current_video_prog
                        updated = True

                    m_merge = re_merging.search(clean_line)
                    if m_merge:
                        with self.lock:
                            job["status"] = "muxing"
                            job["message"] = clean_line
                            job["progress"] = int(m_merge.group(1))
                            job["eta"] = ""
                        updated = True

                    if re_muxing.search(clean_line):
                        with self.lock:
                            job["status"] = "muxing"
                            job["message"] = "Finalizing File (Muxing)..."
                            job["progress"] = 99
                            job["eta"] = ""
                        updated = True
                    
                    if "Muxing success" in clean_line:
                        with self.lock:
                            job["progress"] = 99
                        updated = True

                    if "RuntimeError:" in clean_line or "Error:" in clean_line:
                        if "downloading segment" not in clean_line.lower():
                            error_msg = clean_line.split(":", 1)[1].strip() if ":" in clean_line else clean_line
                            with self.lock:
                                job["message"] = f"Error: {error_msg}"
                            updated = True

                    if re_cleanup.search(clean_line):
                        with self.lock:
                            job["status"] = "cleaning"
                            job["message"] = "Cleaning up..."
                            job["eta"] = ""
                        updated = True

                    # Throttle SSE notification to 250ms during high-frequency lines
                    now = time.time()
                    if updated and (now - last_notify_time > 0.25):
                        self.notify_change()
                        last_notify_time = now
            finally:
                # Guaranteed PTY Master cleanup - eliminates file descriptor leak!
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                    master_fd = None

            process.wait()
            
            with self.lock:
                if process.returncode == 0:
                    if job["progress"] == 0:
                        job["status"] = "failed"
                        job["message"] = "Finished with 0% progress. Check logs."
                        logger.warning(f"Job {job_id} finished with 0% progress.")
                    else:
                        job["status"] = "completed"
                        job["progress"] = 100
                        job["message"] = "Download finished."
                        job["eta"] = ""
                        job["completed_at"] = time.time()
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
            # Ensure slave_fd was closed
            if slave_fd is not None:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            with self.lock:
                job["process_obj"] = None
                job["pid"] = None
                
            # History retention
            with self.lock:
                if job_id in self.active_jobs:
                    self.history[job_id] = self.active_jobs.pop(job_id)
                    
                    # Auto-prune completed jobs: retain at most 5 completed in history
                    completed_items = [
                        (jid, j) for jid, j in self.history.items() if j.get("status") == "completed"
                    ]
                    if len(completed_items) > 5:
                        completed_items.sort(key=lambda x: x[1].get("completed_at") or 0)
                        for old_id, _ in completed_items[:-5]:
                            del self.history[old_id]
            
            # Clean temp dir on success
            if job.get("status") == "completed":
                if job_work_dir and os.path.exists(job_work_dir):
                    try:
                        shutil.rmtree(job_work_dir)
                    except Exception as e:
                        logger.error(f"Failed to cleanup temp dir {job_work_dir}: {e}")

            self.notify_change()


manager = DownloadManager(max_concurrent=2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    manager.set_loop(asyncio.get_running_loop())
    
    # Prune orphaned temp dirs from previous runs
    if os.path.exists(TEMP_BASE):
        try:
            for item in os.listdir(TEMP_BASE):
                p = os.path.join(TEMP_BASE, item)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
            logger.info("Cleaned orphaned temporary directories on startup.")
        except Exception as e:
            logger.warning(f"Error during temp cleanup on startup: {e}")
            
    yield
    
    # Shutdown
    logger.info("Application shutting down, stopping DownloadManager...")
    manager.shutdown()


app = FastAPI(lifespan=lifespan)

# Security Headers Middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# Mount Static and Templates
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    fav_path = os.path.join(BASE_DIR, "static", "favicon.svg")
    if os.path.exists(fav_path):
        return FileResponse(fav_path)
    return JSONResponse(content={}, status_code=404)

@app.get("/")
async def index(request: Request, _: bool = Depends(verify_auth)):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})

@app.post("/download")
async def download(
    url: str = Form(...),
    scene: Optional[str] = Form(None),
    threads: int = Form(10),
    resolution: str = Form("720"),
    names: bool = Form(False),
    covers: bool = Form(False),
    split_scenes: bool = Form(False),
    _: bool = Depends(verify_auth),
):
    # Safely unwrap if called directly in tests/python without FastAPI dependency injection
    if hasattr(split_scenes, "default"):
        split_scenes = split_scenes.default
    if hasattr(names, "default"):
        names = names.default
    if hasattr(covers, "default"):
        covers = covers.default
    if hasattr(threads, "default"):
        threads = threads.default
    if hasattr(resolution, "default"):
        resolution = resolution.default
    if hasattr(scene, "default"):
        scene = scene.default

    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="Invalid URL. Must start with http:// or https://")
    if url.startswith("-"):
        raise HTTPException(status_code=400, detail="Invalid URL format")

    threads = max(1, min(int(threads), 32))

    allowed_resolutions = {"2160", "1440", "1080", "720", "480", "0"}
    if resolution not in allowed_resolutions:
        resolution = "720"

    cleaned_scene = None
    if not split_scenes:
        if scene and scene.strip():
            sc = scene.strip()
            if not sc.isdigit():
                raise HTTPException(status_code=400, detail="Scene must be a valid positive number")
            cleaned_scene = sc
        else:
            cleaned_scene = "1"
        
    output_dir = os.environ.get("DOWNLOAD_DIR", "./downloads")
    
    job_id = manager.add_job(
        url=url,
        threads=threads,
        resolution=resolution,
        scene=cleaned_scene,
        output_dir=output_dir,
        names=bool(names),
        covers=bool(covers),
        split_scenes=bool(split_scenes)
    )
    
    return {"message": "Download queued", "job_id": job_id, "status": "queued"}

@app.post("/cancel/{job_id}")
async def cancel(job_id: str, _: bool = Depends(verify_auth)):
    success = manager.cancel_job(job_id)
    return {"success": success}

@app.post("/restart/{job_id}")
async def restart(job_id: str, _: bool = Depends(verify_auth)):
    new_id = manager.restart_job(job_id)
    if new_id:
        return {"success": True, "new_job_id": new_id}
    return {"success": False, "message": "Job not found"}

@app.delete("/delete/{job_id}")
async def delete(job_id: str, _: bool = Depends(verify_auth)):
    success = manager.delete_job(job_id)
    return {"success": success}

@app.post("/clear-completed")
async def clear_completed(_: bool = Depends(verify_auth)):
    manager.clear_completed_jobs()
    return {"success": True}

@app.get("/logs/{job_id}")
async def get_logs(job_id: str, _: bool = Depends(verify_auth)):
    if not re.match(r"^[a-f0-9]{8}$", job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format")
    logs = manager.get_job_logs(job_id)
    if logs is None:
        raise HTTPException(status_code=404, detail="Job logs not found")
    return {"job_id": job_id, "logs": logs}

@app.get("/status")
async def get_status(_: bool = Depends(verify_auth)):
    return manager.get_status()

@app.get("/stream-status")
async def stream_status(request: Request, _: bool = Depends(verify_auth)):
    """Server-Sent Events (SSE) event-driven status streaming with heartbeat."""
    queue = asyncio.Queue(maxsize=10)
    manager.add_subscriber(queue)
    
    # Push immediate current state to new client
    initial_data = manager.get_status()
    await queue.put(initial_data)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    # 15s keepalive ping
                    yield ": ping\n\n"
        finally:
            manager.remove_subscriber(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

@app.get("/system-info")
async def system_info(_: bool = Depends(verify_auth)):
    version = "Unknown"
    date = "Unknown"
    
    try:
        manifest_path = os.path.join(PROJECT_ROOT, "manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r') as f:
                data = json.load(f)
                version = data.get("last_known_good_sha", "Unknown")[:7]
                date = data.get("last_update_timestamp", "Unknown")
    except Exception as e:
        logger.error(f"Error reading system info: {e}")
        
    return {"version": version, "date": date, "auth_enabled": AUTH_ENABLED}
