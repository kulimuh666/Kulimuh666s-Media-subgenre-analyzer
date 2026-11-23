"""Local llama.cpp runtime for direct analysis and tag shortening."""
import json
import logging
import os
import ssl
import subprocess
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger("vdj_server")

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
RUNTIME = ROOT / "runtime"

ANALYZE_PORT = 8081
SUMMARY_PORT = 8082

CATALOG = {
    "runtime": {
        "label": "llama.cpp CUDA 12 (b12651)",
        "files": [
            {
                "url": "https://github.com/ggml-org/llama.cpp/releases/download/b12651/llama-b12651-bin-win-cuda-12.4-x64.zip",
                "name": "llama-b12651-bin-win-cuda-12.4-x64.zip",
            },
            {
                "url": "https://github.com/ggml-org/llama.cpp/releases/download/b12651/cudart-llama-bin-win-cuda-12.4-x64.zip",
                "name": "cudart-llama-bin-win-cuda-12.4-x64.zip",
            },
        ],
    },
    "analyze": {
        "label": "Qwen2.5-3B-Instruct Q4_K_M",
        "files": [
            {
                "url": "https://huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF/resolve/main/Qwen2.5-3B-Instruct-Q4_K_M.gguf",
                "name": "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
            },
        ],
    },
    "summarize": {
        "label": "Qwen2.5-1.5B-Instruct Q4_K_M",
        "files": [
            {
                "url": "https://huggingface.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
                "name": "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
            },
        ],
    },
    "audio": {
        "label": "Discogs-EffNet and MTT-MusicNN",
        "files": [
            {
                "url": "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bsdynamic-1.onnx",
                "name": "discogs-effnet-bsdynamic-1.onnx",
            },
            {
                "url": "https://essentia.upf.edu/models/music-style-classification/discogs-effnet/discogs-effnet-bs64-1.json",
                "name": "discogs-effnet-bs64-1.json",
            },
            {
                "url": "https://essentia.upf.edu/models/autotagging/mtt/mtt-musicnn-1.onnx",
                "name": "mtt-musicnn-1.onnx",
            },
            {
                "url": "https://essentia.upf.edu/models/autotagging/mtt/mtt-musicnn-1.json",
                "name": "mtt-musicnn-1.json",
            },
            {
                "url": "https://essentia.upf.edu/models/feature-extractors/maest/discogs-maest-30s-pw-519l-2.onnx",
                "name": "discogs-maest-30s-pw-519l-2.onnx",
            },
            {
                "url": "https://essentia.upf.edu/models/feature-extractors/maest/discogs-maest-30s-pw-519l-2.json",
                "name": "discogs-maest-30s-pw-519l-2.json",
            },
        ],
    },
}

_lock = threading.Lock()
_jobs = {}


def _server_exe():
    for name in ("llama-server.exe", "llama.exe"):
        found = list(RUNTIME.rglob(name))
        if found:
            return found[0]
    return None


def _model_path(which):
    name = CATALOG[which]["files"][0]["name"]
    return MODELS / name


def _listening(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def status():
    jobs = {}
    with _lock:
        jobs = {key: dict(value) for key, value in _jobs.items()}
    return {
        "runtime": _server_exe() is not None,
        "analyze_model": _model_path("analyze").is_file(),
        "summarize_model": _model_path("summarize").is_file(),
        "audio_models": all((MODELS / spec["name"]).is_file() for spec in CATALOG["audio"]["files"]),
        "analyze_up": _listening(ANALYZE_PORT),
        "summarize_up": _listening(SUMMARY_PORT),
        "models_dir": str(MODELS),
        "runtime_dir": str(RUNTIME),
        "jobs": jobs,
        "catalog": {
            key: {"label": item["label"], "urls": [f["url"] for f in item["files"]]}
            for key, item in CATALOG.items()
        },
    }


def _set_job(which, **fields):
    with _lock:
        job = _jobs.setdefault(which, {})
        job.update(fields)


def _write_http(url, part: Path, label: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "vdj-analyzer"})
    with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context()) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with part.open("wb") as out:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                _set_job(label, bytes=done, total=total, state="downloading")


def _write_curl(url, part: Path) -> None:
    proc = subprocess.run(
        ["curl.exe", "-L", "--fail", "--retry", "2", "-o", str(part), url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    if proc.returncode != 0 or not part.is_file() or part.stat().st_size <= 0:
        detail = ((proc.stderr or proc.stdout) or "curl.exe fehlgeschlagen").strip()[-300:]
        raise RuntimeError(detail)


def _download_one(url, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        _write_http(url, part, dest.name)
    except Exception as exc:
        if part.exists():
            part.unlink()
        text = str(exc).upper()
        if "CERTIFICATE" not in text and "SSL" not in text:
            raise
        print(f"Python-Zertifikat abgelehnt, lade mit curl.exe: {dest.name}")
        _write_curl(url, part)
    part.replace(dest)


def _run_download(which):
    item = CATALOG[which]
    try:
        for spec in item["files"]:
            dest = (RUNTIME if which == "runtime" else MODELS) / spec["name"]
            if dest.is_file() and dest.stat().st_size > 0:
                logger.info(f"LLM already present: {dest}")
                continue
            logger.info(f"LLM download {spec['url']} -> {dest}")
            _set_job(which, state="downloading", file=spec["name"], error="")
            _download_one(spec["url"], dest)
            if which == "runtime" and dest.suffix.lower() == ".zip":
                with zipfile.ZipFile(dest) as zf:
                    zf.extractall(RUNTIME)
                logger.info(f"LLM extracted {dest}")
        _set_job(which, state="done", error="")
        logger.info(f"LLM download done: {which}")
    except Exception as exc:
        logger.error(f"LLM download failed {which}: {exc}")
        _set_job(which, state="error", error=str(exc))


def ensure_audio_models() -> tuple[bool, str]:
    missing = []
    for spec in CATALOG["audio"]["files"]:
        dest = MODELS / spec["name"]
        if not dest.is_file() or dest.stat().st_size <= 0:
            missing.append(spec)
    if not missing:
        return True, ""
    print("Lade Audio-Modelle nach models ...")
    try:
        for spec in missing:
            dest = MODELS / spec["name"]
            print(f"  {spec['name']}")
            _download_one(spec["url"], dest)
    except Exception as exc:
        return False, str(exc)
    return True, ""


def start_download(which):
    if which not in CATALOG:
        raise ValueError("unknown download")
    with _lock:
        if _jobs.get(which, {}).get("state") == "downloading":
            return False
        _jobs[which] = {"state": "downloading", "error": ""}
    threading.Thread(target=_run_download, args=(which,), daemon=True).start()
    return True


def _spawn(model: Path, port: int, alias: str):
    exe = _server_exe()
    if exe is None:
        raise FileNotFoundError("llama-server.exe fehlt. Zuerst die Runtime laden.")
    if not model.is_file():
        raise FileNotFoundError(f"Modell fehlt: {model.name}")
    if _listening(port):
        return "already"
    log_path = RUNTIME / f"{alias}.log"
    log_file = log_path.open("a", encoding="utf-8")
    cmd = [
        str(exe),
        "-m", str(model),
        "--host", "127.0.0.1",
        "--port", str(port),
        "-c", "4096",
        "-ngl", "99",
        "--alias", alias,
    ]
    env = os.environ.copy()
    env["PATH"] = str(RUNTIME) + os.pathsep + str(exe.parent) + os.pathsep + env.get("PATH", "")
    subprocess.Popen(
        cmd,
        cwd=str(exe.parent),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    logger.info(f"LLM start {alias} port {port} model {model.name}")
    return "started"


def start_servers(summarize=True):
    started = {"analyze": _spawn(_model_path("analyze"), ANALYZE_PORT, "analyzer")}
    if summarize:
        started["summarize"] = _spawn(_model_path("summarize"), SUMMARY_PORT, "summarizer")
    return started


def shorten(text, timeout=120):
    """Ask the small model to shorten a tag list. Keep only tokens it was given."""
    source = [part.strip() for part in (text or "").split("/") if part.strip()]
    if len(source) < 2 or not _listening(SUMMARY_PORT):
        return text
    payload = {
        "model": "summarizer",
        "temperature": 0,
        "max_tokens": 120,
        "messages": [
            {
                "role": "system",
                "content": "Du kürzt Stil-Listen. Antworte nur mit den Stilen, getrennt durch /. Erfinde nichts. Lass Wiederholungen und kürzere Stile weg, die in einem genaueren schon stecken.",
            },
            {"role": "user", "content": " / ".join(source)},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{SUMMARY_PORT}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, json.JSONDecodeError, IndexError) as exc:
        logger.warning(f"LLM summarize failed, keeping original: {exc}")
        return text
    allowed = {part.lower(): part for part in source}
    kept = []
    for part in content.replace(",", "/").split("/"):
        key = part.strip().lower()
        if key in allowed and allowed[key] not in kept:
            kept.append(allowed[key])
    if not kept:
        return text
    logger.info(f"LLM summarize {text!r} -> {'/'.join(kept)!r}")
    return "/".join(kept)
