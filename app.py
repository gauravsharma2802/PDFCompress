"""
Flask web app for PDF compression.

Routes:
  GET  /                  — Upload page
  POST /upload            — Receive one or more ZIPs, validate, return job_id
  GET  /process/<job_id>  — SSE: compress in background thread, stream progress
  GET  /download/<job_id> — Serve compressed result, clean up
"""

import json
import os
import queue
import shutil
import tempfile
import threading
import time
import uuid
import zipfile

from flask import (
    Flask, render_template, request, jsonify, Response, send_file,
)

from compress import check_dependencies, process_zip_for_web

app = Flask(__name__)

MAX_UPLOAD_BYTES = 500 * 1024 * 1024       # 500 MB per file
MAX_TOTAL_UPLOAD_BYTES = 2 * 1024**3       # 2 GB total across all files
MAX_UNCOMPRESSED_BYTES = 2 * 1024**3       # 2 GB uncompressed per ZIP
UPLOAD_DIR = tempfile.mkdtemp(prefix="pdf_webapp_")

# In-memory job store
# job_id -> {input_paths: [], output_paths: [], original_names: [],
#             queue, result, created_at}
jobs: dict = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cleanup: remove stale jobs older than 1 hour
# ---------------------------------------------------------------------------

def _cleanup_stale_jobs():
    """Remove jobs older than 1 hour. Called periodically."""
    cutoff = time.time() - 3600
    with jobs_lock:
        stale = [jid for jid, j in jobs.items() if j["created_at"] < cutoff]
        for jid in stale:
            _remove_job_files(jid)
            del jobs[jid]


def _remove_job_files(job_id: str):
    """Delete all input and output files for a job."""
    job = jobs.get(job_id)
    if not job:
        return
    for path in job.get("input_paths", []):
        if path and os.path.exists(path):
            os.remove(path)
    for path in job.get("output_paths", []):
        if path and os.path.exists(path):
            os.remove(path)
    bundle = job.get("bundle_path")
    if bundle and os.path.exists(bundle):
        os.remove(bundle)


def _start_cleanup_timer():
    """Run cleanup every 30 minutes."""
    _cleanup_stale_jobs()
    t = threading.Timer(1800, _start_cleanup_timer)
    t.daemon = True
    t.start()


_start_cleanup_timer()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    dep_error = check_dependencies()
    return render_template("index.html", dep_error=dep_error)


@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return jsonify(error="No files provided"), 400

    job_id = uuid.uuid4().hex[:12]
    input_paths = []
    output_paths = []
    original_names = []
    total_pdf_count = 0
    total_file_size = 0

    for f in files:
        if not f.filename:
            continue
        if not f.filename.lower().endswith(".zip"):
            # Clean up already-saved files
            for p in input_paths:
                os.remove(p)
            return jsonify(error=f"'{f.filename}' is not a ZIP file. Only ZIP files are accepted."), 400

        idx = len(input_paths)
        input_path = os.path.join(UPLOAD_DIR, f"{job_id}_input_{idx}.zip")
        f.save(input_path)

        file_size = os.path.getsize(input_path)
        if file_size > MAX_UPLOAD_BYTES:
            for p in input_paths:
                os.remove(p)
            os.remove(input_path)
            return jsonify(error=f"'{f.filename}' is too large ({file_size // (1024*1024)} MB). Max is 500 MB per file."), 400

        total_file_size += file_size
        if total_file_size > MAX_TOTAL_UPLOAD_BYTES:
            for p in input_paths:
                os.remove(p)
            os.remove(input_path)
            return jsonify(error="Total upload size exceeds 2 GB limit."), 400

        if not zipfile.is_zipfile(input_path):
            for p in input_paths:
                os.remove(p)
            os.remove(input_path)
            return jsonify(error=f"'{f.filename}' is not a valid ZIP archive."), 400

        try:
            with zipfile.ZipFile(input_path, "r") as zf:
                entries = zf.namelist()
                pdf_count = sum(1 for e in entries if e.lower().endswith(".pdf"))
                if pdf_count == 0:
                    for p in input_paths:
                        os.remove(p)
                    os.remove(input_path)
                    return jsonify(error=f"'{f.filename}' contains no PDF files."), 400

                total_uncompressed = sum(info.file_size for info in zf.infolist())
                if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    for p in input_paths:
                        os.remove(p)
                    os.remove(input_path)
                    return jsonify(error=f"'{f.filename}' contents too large when uncompressed (max 2 GB)."), 400

                total_pdf_count += pdf_count
        except zipfile.BadZipFile:
            for p in input_paths:
                os.remove(p)
            os.remove(input_path)
            return jsonify(error=f"'{f.filename}' is a corrupted ZIP file."), 400

        output_path = os.path.join(UPLOAD_DIR, f"{job_id}_output_{idx}.zip")
        input_paths.append(input_path)
        output_paths.append(output_path)
        original_names.append(f.filename)

    if not input_paths:
        return jsonify(error="No valid files provided"), 400

    with jobs_lock:
        jobs[job_id] = {
            "input_paths": input_paths,
            "output_paths": output_paths,
            "original_names": original_names,
            "queue": queue.Queue(),
            "result": None,
            "bundle_path": None,
            "created_at": time.time(),
        }

    return jsonify(
        job_id=job_id,
        zip_count=len(input_paths),
        pdf_count=total_pdf_count,
        file_size=total_file_size,
    )


@app.route("/process/<job_id>")
def process(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="Job not found"), 404

    q = job["queue"]

    def run_compression():
        input_paths = job["input_paths"]
        output_paths = job["output_paths"]
        original_names = job["original_names"]
        zip_count = len(input_paths)

        totals = {
            "original_bytes": 0,
            "compressed_bytes": 0,
            "total_pdfs": 0,
            "compressed_pdfs": 0,
            "skipped_pdfs": 0,
            "failed_pdfs": 0,
        }
        errors = []

        for zip_idx in range(zip_count):
            zip_name = original_names[zip_idx]

            q.put({
                "type": "zip_start",
                "zip_index": zip_idx + 1,
                "zip_count": zip_count,
                "zip_name": zip_name,
            })

            def progress_callback(current, total, filename):
                q.put({
                    "type": "progress",
                    "zip_index": zip_idx + 1,
                    "zip_count": zip_count,
                    "zip_name": zip_name,
                    "current": current,
                    "total": total,
                    "filename": os.path.basename(filename),
                })

            result = process_zip_for_web(
                input_paths[zip_idx], output_paths[zip_idx], progress_callback
            )

            if result.error:
                errors.append(f"{zip_name}: {result.error}")

            totals["original_bytes"] += result.original_total_bytes
            totals["compressed_bytes"] += result.compressed_total_bytes
            totals["total_pdfs"] += result.total_pdfs
            totals["compressed_pdfs"] += result.compressed_pdfs
            totals["skipped_pdfs"] += result.skipped_pdfs
            totals["failed_pdfs"] += result.failed_pdfs

        # If multiple ZIPs, bundle all outputs into one ZIP
        if zip_count > 1:
            bundle_path = os.path.join(UPLOAD_DIR, f"{job_id}_bundle.zip")
            with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_STORED) as bundle:
                for i, out_path in enumerate(output_paths):
                    if os.path.exists(out_path):
                        # Use original filename with _compressed suffix
                        name = original_names[i]
                        base, ext = os.path.splitext(name)
                        arc_name = f"{base}_compressed{ext}"
                        bundle.write(out_path, arc_name)
            with jobs_lock:
                job["bundle_path"] = bundle_path
        else:
            with jobs_lock:
                job["bundle_path"] = None

        if errors:
            q.put({"type": "error", "message": "; ".join(errors)})
        else:
            q.put({
                "type": "complete",
                "zip_count": zip_count,
                **totals,
            })

        q.put(None)  # sentinel

    thread = threading.Thread(target=run_compression, daemon=True)
    thread.start()

    def event_stream():
        while True:
            try:
                msg = q.get(timeout=600)  # 10 min timeout
            except queue.Empty:
                yield "data: {\"type\": \"error\", \"message\": \"Processing timed out\"}\n\n"
                return

            if msg is None:
                return
            yield f"data: {json.dumps(msg)}\n\n"

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/download/<job_id>")
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="Job not found"), 404

    # If multiple ZIPs, serve the bundle; otherwise serve the single output
    if job.get("bundle_path") and os.path.exists(job["bundle_path"]):
        serve_path = job["bundle_path"]
        download_name = "compressed_pdfs.zip"
    elif job["output_paths"] and os.path.exists(job["output_paths"][0]):
        serve_path = job["output_paths"][0]
        base, ext = os.path.splitext(job["original_names"][0])
        download_name = f"{base}_compressed{ext}"
    else:
        return jsonify(error="Compressed file not available"), 404

    def cleanup_after_send():
        """Remove job files after download."""
        time.sleep(2)
        with jobs_lock:
            _remove_job_files(job_id)
            if job_id in jobs:
                del jobs[job_id]

    threading.Thread(target=cleanup_after_send, daemon=True).start()

    return send_file(
        serve_path,
        as_attachment=True,
        download_name=download_name,
        mimetype="application/zip",
    )


# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------

app.config["MAX_CONTENT_LENGTH"] = MAX_TOTAL_UPLOAD_BYTES + 1024


if __name__ == "__main__":
    app.run(debug=True, port=8080, threaded=True)
