"""
VDJ Subgenre Analyzer – Backend Server (server.py)
===================================================
HTTP API Server auf Port 8765 für die VDJ-Analyse.

Endpunkte:
    GET  /api/config          - Konfiguration lesen
    POST /api/config          - Konfiguration schreiben
    GET  /api/files?dir=X     - Audio-Dateien auflisten
    POST /api/analyze         - Analyse starten (optional mit resume)
    GET  /api/log             - Log-Inhalte lesen
    GET  /api/status          - Status + Fortschritt

State-Saving: state.json alle 30 Sekunden automatisch.
Crash-Resilienz: state.json wird beim Start geladen, Fortsetzung angeboten.
Logging: Alle API-Aufrufe in analyze.log.
"""

from __future__ import annotations

import json
import logging
import os
import re
import io
import zipfile
import ctypes
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

# ── Pfade ────────────────────────────────────────────────────────────────

PLUGIN_DIR = Path(__file__).resolve().parent
import llm_local
CONFIG_PATH = PLUGIN_DIR / "config.json"
STATE_PATH = PLUGIN_DIR / "state.json"
LOG_PATH = PLUGIN_DIR / "analyze.log"
SERVER_PORT = 8765

# ── Logging Setup ────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("vdj_server")
    if logger.handlers:
        return logger
    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.setLevel(logging.DEBUG)
    return logger

logger = _setup_logger()


# ── Config Manager (inline, from config_manager.py) ─────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "hermes_url": "http://127.0.0.1:8642/v1/chat/completions",
    "api_key": "",
    "workers": 1,
    "target_dir": "",
    "dry_run": False,
    "limit": None,
    "timeout": 1800,
    "analyze_mode": "web",
    "audio_model": "effnet",
    "extra_tag": "user1",
    "llm_backend": "hermes",
    "summarize": False,
}

_config_lock = threading.Lock()


def _load_config_raw(path: Path | None = None) -> Dict[str, Any]:
    cfg_path = path or CONFIG_PATH
    if not cfg_path.exists():
        return dict(DEFAULT_CONFIG)
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    merged = {**DEFAULT_CONFIG, **raw}
    merged["workers"] = int(merged.get("workers", DEFAULT_CONFIG["workers"]))
    merged["dry_run"] = bool(merged.get("dry_run", DEFAULT_CONFIG["dry_run"]))
    if merged.get("limit") is not None:
        merged["limit"] = int(merged["limit"])
    raw_timeout = merged.get("timeout", 1800)
    try:
        merged["timeout"] = int(raw_timeout)
    except (TypeError, ValueError):
        merged["timeout"] = 1800
    if merged.get("analyze_mode") not in ("web", "direct", "audio"):
        merged["analyze_mode"] = "web"
    if merged.get("audio_model") not in ("effnet", "maest", "both"):
        merged["audio_model"] = "effnet"
    if merged.get("extra_tag") not in ("user1", "albumartist", "off"):
        merged["extra_tag"] = "user1"
    if merged.get("llm_backend") not in ("hermes", "local"):
        merged["llm_backend"] = "hermes"
    merged["summarize"] = bool(merged.get("summarize", False))
    return merged


def _save_config_raw(cfg: Dict[str, Any], path: Path | None = None) -> None:
    cfg_path = path or CONFIG_PATH
    tmp = cfg_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    tmp.replace(cfg_path)


def load_config() -> Dict[str, Any]:
    with _config_lock:
        return _load_config_raw()


def save_config(cfg: Dict[str, Any]) -> None:
    with _config_lock:
        _save_config_raw(cfg)


# ── State Manager (inline, from state_manager.py) ───────────────────────

_state_lock = threading.Lock()
_auto_save_timer: Optional[threading.Timer] = None
_auto_save_running = False
_state_data: Dict[str, Any] = {
    "last_update": "",
    "total_files": 0,
    "completed": 0,
    "tracks": [],
}


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_state() -> Dict[str, Any]:
    with _state_lock:
        if not STATE_PATH.exists():
            return get_state_copy()
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            global _state_data
            _state_data = {**_state_data, **raw}
        except (json.JSONDecodeError, KeyError):
            pass
    return get_state_copy()


def save_state() -> Path:
    with _state_lock:
        _state_data["last_update"] = datetime.now(timezone.utc).isoformat()
        data_to_write = dict(_state_data)
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data_to_write, fh, indent=2, ensure_ascii=False)
    tmp.replace(STATE_PATH)
    return STATE_PATH


def get_state_copy() -> Dict[str, Any]:
    with _state_lock:
        return dict(_state_data)


def start_auto_save() -> None:
    global _auto_save_running, _auto_save_timer
    if _auto_save_running:
        return
    _auto_save_running = True
    _schedule_next_save()


def stop_auto_save() -> None:
    global _auto_save_running, _auto_save_timer
    _auto_save_running = False
    if _auto_save_timer is not None:
        _auto_save_timer.cancel()
        _auto_save_timer = None


def _schedule_next_save() -> None:
    if not _auto_save_running:
        return
    _auto_save_timer = threading.Timer(30, _auto_save_tick)
    _auto_save_timer.daemon = True
    _auto_save_timer.start()


def _auto_save_tick() -> None:
    if not _auto_save_running:
        return
    try:
        save_state()
    except Exception:
        pass
    finally:
        _schedule_next_save()


# ── Track helpers (inline, from state_manager.py) ───────────────────────

def add_track(filepath: str) -> Dict[str, Any]:
    with _state_lock:
        for t in _state_data["tracks"]:
            if t.get("filepath") == filepath:
                return dict(t)
        track = {
            "filepath": filepath,
            "status": "pending",
            "subgenres": "",
            "genre": "",
            "jahr": 0,
            "error": "",
        }
        _state_data["tracks"].append(track)
        _update_counters()
        return dict(track)


def update_track(filepath: str, **fields: Any) -> Optional[Dict[str, Any]]:
    with _state_lock:
        for t in _state_data["tracks"]:
            if t.get("filepath") == filepath:
                for k, v in fields.items():
                    if k in ("status", "subgenres", "genre", "jahr", "error", "artist", "title"):
                        t[k] = v
                _update_counters()
                return dict(t)
        return None


def set_status(filepath: str, status: str, **extra: Any) -> Optional[Dict[str, Any]]:
    valid = ("pending", "analyzing", "success", "error", "skipped", "unverified", "unsupported", "keygen")
    if status not in valid:
        raise ValueError(f"Invalid status {status!r}")
    with _state_lock:
        for t in _state_data["tracks"]:
            if t.get("filepath") == filepath:
                t["status"] = status
                for k, v in extra.items():
                    if k in ("subgenres", "genre", "jahr", "error", "artist", "title"):
                        t[k] = v
                _update_counters()
                return dict(t)
        return None


def _update_counters() -> None:
    tracks = _state_data.get("tracks", [])
    _state_data["total_files"] = len(tracks)
    _state_data["completed"] = sum(1 for t in tracks if t.get("status") == "success")


# ── Audio file discovery (from direct_analyzer.py logic) ────────────────

SUPPORTED_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.mp4', '.aac', '.ogg', '.opus'}


def list_audio_paths(root_dir: str) -> List[str]:
    """Find audio files without reading tags."""
    root_path = Path(root_dir)
    if not root_path.exists():
        return []
    found = []
    for ext in SUPPORTED_EXTENSIONS:
        found.extend(str(p) for p in root_path.rglob(f'*{ext}'))
    found.sort()
    return found


def find_audio_files(root_dir: str) -> List[str]:
    """Recursively find audio files, shortest genre tag first (empty first)."""
    root_path = Path(root_dir)
    if not root_path.exists():
        return []

    audio_files = []
    for ext in SUPPORTED_EXTENSIONS:
        audio_files.extend(root_path.rglob(f'*{ext}'))

    ranked = []
    for p in audio_files:
        try:
            genre_len = len(read_tags(str(p)).get('genre', '') or '')
        except Exception:
            genre_len = 0
        ranked.append((genre_len, str(p)))

    ranked.sort(key=lambda item: (item[0], item[1]))
    return [path for _, path in ranked]


def tag_support(filepath: str) -> Optional[str]:
    """Return why this file cannot be tagged, or None if it can. Checked before Hermes."""
    suffix = Path(filepath).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return f"Format {suffix or 'ohne Endung'} wird nicht unterstützt"
    if not mutagen_ready():
        return "mutagen not available"
    try:
        if suffix == '.mp3' and MP3:
            MP3(filepath, ID3=EasyID3)
        elif suffix == '.flac' and FLAC:
            FLAC(filepath)
        elif suffix in ('.m4a', '.aac', '.mp4') and M4A:
            M4A(filepath)
        elif suffix == '.ogg' and OggVorbis:
            OggVorbis(filepath)
        elif suffix == '.opus' and OggOpus:
            OggOpus(filepath)
        else:
            return f"Format {suffix} wird nicht unterstützt"
    except Exception as exc:
        return f"{suffix} kann nicht geöffnet werden: {exc}"
    return None


def is_keygen(filepath: str) -> bool:
    return 'keygen' in filepath.lower()


# ── Tag reading (from direct_analyzer.py) ───────────────────────────────

def ensure_mutagen() -> bool:
    """Import mutagen, install it with this interpreter if it is missing."""
    try:
        import mutagen  # noqa: F401
        print(f"mutagen {mutagen.version_string} OK")
        return True
    except ImportError:
        print("mutagen fehlt, installiere...")
        rc = subprocess.call([sys.executable, "-m", "pip", "install", "mutagen"])
        if rc != 0:
            print("mutagen Installation fehlgeschlagen")
            return False
        import mutagen  # noqa: F401
        print(f"mutagen {mutagen.version_string} installiert")
        return True


def _load_mutagen() -> None:
    global EasyID3, MP3, FLAC, M4A, OggVorbis, OggOpus
    if not ensure_mutagen():
        EasyID3 = MP3 = FLAC = M4A = OggVorbis = OggOpus = None
        return
    from mutagen.easyid3 import EasyID3 as _EasyID3
    from mutagen.mp3 import MP3 as _MP3
    from mutagen.flac import FLAC as _FLAC
    from mutagen.mp4 import MP4 as _MP4
    from mutagen.oggvorbis import OggVorbis as _OggVorbis
    from mutagen.oggopus import OggOpus as _OggOpus
    EasyID3, MP3, FLAC, M4A, OggVorbis, OggOpus = (
        _EasyID3, _MP3, _FLAC, _MP4, _OggVorbis, _OggOpus,
    )


_load_mutagen()


def mutagen_ready() -> bool:
    """Reload mutagen if this process started before it was installed."""
    if EasyID3 is not None:
        return True
    _load_mutagen()
    return EasyID3 is not None


def read_tags(filepath: str) -> Dict[str, Any]:
    """Read tags from audio file."""
    tags = {
        'genre': '',
        'artist': '',
        'title': '',
        'composer': '',
        'date': '',
        'user1': '',
        'albumartist': '',
        'filename': Path(filepath).name,
        'filepath': filepath,
    }

    if not mutagen_ready():
        return tags

    try:
        p = Path(filepath)
        suffix = p.suffix.lower()

        audio = None
        if suffix == '.mp3' and MP3:
            audio = MP3(str(p), ID3=EasyID3)
        elif suffix == '.flac' and FLAC:
            audio = FLAC(str(p))
        elif suffix in ('.m4a', '.aac', '.mp4') and M4A:
            audio = M4A(str(p))
        elif suffix == '.ogg' and OggVorbis:
            audio = OggVorbis(str(p))
        elif suffix == '.opus' and OggOpus:
            audio = OggOpus(str(p))

        if not audio:
            return tags

        key_map = {
            'genre': '\xa9gen',
            'artist': '\xa9ART',
            'title': '\xa9nam',
            'composer': '\xa9wrt',
            'date': '\xa9day',
        } if suffix in ('.m4a', '.aac', '.mp4') else {}

        for key in ('genre', 'artist', 'title', 'composer', 'date'):
            try:
                stored = key_map.get(key, key)
                val = str(audio.get(stored, [''])[0] or '')
                tags[key] = val
            except (IndexError, KeyError, TypeError):
                pass

        if suffix == '.mp3' and not (tags['artist'] and tags['title']):
            try:
                from mutagen.id3 import ID3
                raw = ID3(str(p))
                if not tags['artist']:
                    frame = raw.get('TPE1') or raw.get('TPE2')
                    tags['artist'] = str(frame.text[0]).strip() if frame and frame.text else ''
                if not tags['title']:
                    frame = raw.get('TIT2')
                    tags['title'] = str(frame.text[0]).strip() if frame and frame.text else ''
            except Exception:
                pass
        elif suffix in ('.flac', '.ogg', '.opus') and not (tags['artist'] and tags['title']):
            if not tags['artist']:
                tags['artist'] = str(audio.get('ARTIST', audio.get('albumartist', ['']) or [''])[0] or '')
            if not tags['title']:
                tags['title'] = str(audio.get('TITLE', [''])[0] or '')

    except Exception as e:
        logger.warning(f"read_tags error for {filepath}: {e}")

    user1, album = read_extra_tags(filepath)
    tags['user1'] = user1
    tags['albumartist'] = album
    return tags


# ── Hermes API call (from direct_analyzer.py) ───────────────────────────

HERMES_URL = "http://127.0.0.1:8642/v1/chat/completions"
API_KEY = ""


def send_to_hermes(tags: Dict[str, Any], mode: Optional[str] = None, backend: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Send track info to Hermes API and parse JSON response."""
    prompt = (
        f"Du bist ein Musik-Experte für DJ-Software. Analysiere diesen Track.\n\n"
        f"**Künstler:** {tags['artist']}\n"
        f"**Titel:** {tags['title']}\n"
        f"**Genre (eingetragen):** {tags.get('genre', 'N/A')}\n"
        f"**Komponist (bereits gesetzt):** {tags.get('composer', 'N/A')}\n"
        f"**Dateiname:** {tags['filename']}\n"
        f"**Ordnerpfad:** {tags.get('filepath', '')}\n\n"
    )
    cfg = load_config()
    mode = mode or active_mode()
    if mode == "direct":
        prompt += (
            "Analysiere nur mit den Angaben oben. Keine Websuche, keine Tools. "
            "Schätze die Subgenres aus Dateiname, Ordner und Tags.\n\n"
            "Regeln:\n"
            "- subgenres = Stile, die aus Dateiname, Ordner oder vorhandenen Tags hervorgehen.\n"
            "- einordnung = deine eigene DJ-Einordnung, mehrere mit \"/\" vom gröbsten zum feinsten.\n"
            "- Keinen Stil wiederholen. Psytrance statt Psychedelic Trance. "
            "Nur den genaueren Stil, zum Beispiel Peak Time Techno/Driving Techno.\n"
            "- Jahr nur wenn es in den Angaben steht, sonst null.\n\n"
            "Antwort NUR als JSON ohne Markdown:\n"
            '{"subgenres": "Stil1/Stil2", "einordnung": "Stil1/Stil2", "jahr": YYYY oder null}'
        )
    else:
        prompt += (
            "Suche zuerst: nutze Websuche und die Skills music-track-lookup und "
            "song-request, bevor du antwortest. Verlass dich nicht nur auf dein Gedächtnis.\n\n"
            "Regeln, strikt einhalten:\n"
        f"- Verwende NUR Informationen, die du zu genau diesem Track belegen kannst "
        f"(Veröffentlichung, Label, Release-Liste, eigene sichere Kenntnis).\n"
        f"- Dateiname und Ordnerpfad sind Quellen zur Identifikation. "
        f"Künstler, Titel, Remixer, Label, Album und Jahr stehen oft im Dateinamen. "
        f"Ein übergeordneter Ordner ist oft der Künstler oder das Album, "
        f"nicht nur ein Ablageort. Nutze beides für die Suche.\n"
        f"- Zwei Felder, strikt getrennt:\n"
        f"  subgenres = nur Stile, für die du eine Quelle gefunden hast "
        f"(Tag, Label-Angabe, Release-Beschreibung).\n"
        f"  einordnung = deine eigene stilistische Einordnung als DJ: alle Subgenres, "
        f"die zu diesem Track und zum Remixer passen, auch ohne einzelne Quelle. "
        f"Mehrere mit \"/\" trennen, vom gröbsten zum feinsten. "
        f"Keinen Stil wiederholen, der schon in subgenres oder einordnung steht. "
        f"Psytrance ist die Kurzform von Psychedelic Trance. Nur Psytrance schreiben, nicht beide. "
        f"Wenn ein genauerer Stil einen kürzeren schon enthält, nur den genaueren schreiben, "
        f"zum Beispiel Peak Time Techno/Driving Techno, nicht zusätzlich Techno, Peak Time oder Driving.\n"
        f"- Das Jahr nur angeben, wenn das Erscheinungsjahr dieses Tracks belegt ist. "
        f"Sonst null. Kein ungefähres Jahr, kein Jahrzehnt.\n"
        f"- Wenn du nichts belegen kannst, setze subgenres auf \"\" und jahr auf null.\n\n"
        f"Antwort NUR als JSON ohne Markdown:\n"
        f'{{\n  "subgenres": "belegt1/belegt2",\n  "einordnung": "Stil1/Stil2",\n  "jahr": YYYY oder null\n}}'
    )

    cfg = load_config()
    mode = mode or active_mode()
    if backend is None:
        backend = cfg.get("llm_backend") or "hermes"
    local = mode == "direct" and backend == "local"
    url = "http://127.0.0.1:8081/v1/chat/completions" if local else cfg.get("hermes_url", HERMES_URL)
    key = "local" if local else (cfg.get("api_key", API_KEY) or API_KEY)
    model_name = "analyzer" if local else "hermes-agent"
    try:
        timeout = int(cfg.get("timeout", 1800))
    except (TypeError, ValueError):
        timeout = 1800
    if timeout <= 0:
        timeout = None
    if mode == "direct":
        system = "Du bist ein Musik-Experte. Antworte NUR als JSON. Keine Tools, keine Websuche."
        max_tokens = 600
    else:
        system = "Du bist ein Musik-Experte für DJ-Software. Antworte NUR als JSON. Nenne alle belegbaren Subgenres, erfinde aber keine."
        max_tokens = 8000

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }

    max_retries = 3
    for attempt in range(max_retries):
        result_text = ""
        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {key}',
                    'User-Agent': 'VDJ-Server/1.0',
                },
                method='POST',
            )

            http_status = 0
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result_text = resp.read().decode('utf-8')
                http_status = resp.status

            # Log API call
            logger.info(
                f"HERMES_REQ | mode={mode} timeout={timeout} | URL={url} | REQ={json.dumps(payload, ensure_ascii=False)[:500]}"
            )
            logger.info(f"HERMES_RESP | HTTP {http_status} | RESP={result_text[:800]}")

            parsed = json.loads(result_text)
            if 'choices' in parsed:
                message = parsed['choices'][0].get('message') or {}
                content = message.get('content') or ''
                finish = parsed['choices'][0].get('finish_reason')
                hermes_meta = parsed.get('hermes') or {}
                logger.info(
                    f"HERMES_CONTENT | finish={finish} | error_code={hermes_meta.get('error_code')} | "
                    f"len={len(content)} | {content[:800]}"
                )
            else:
                logger.warning(f"No choices in API response: {result_text[:800]}")
                return None

            # Strip markdown code blocks
            if '```json' in content:
                content = content.split('```json')[1].split('```')[0]
            elif '```' in content:
                content = content.split('```')[1].split('```')[0]

            return json.loads(content)

        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5
                logger.info(f"HTTP 409 Conflict, retry in {wait_time}s")
                time.sleep(wait_time)
            else:
                logger.error(f"HTTP error {e.code}: {e.reason}")
                return None
        except urllib.error.URLError as e:
            logger.error(f"Hermes connection failed: {e.reason}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from Hermes: {e} | snippet={result_text[:800]}")
            return None
        except Exception as e:
            logger.error(f"Error in Hermes request: {e}")
            return None

    return None


# ── Subgenre write (from direct_analyzer.py) ────────────────────────────

SHORT_TAGS = {
    "psytrance": "Psytrance",
    "psy trance": "Psytrance",
    "psychedelic trance": "Psytrance",
}


def join_tags(*chunks: str) -> str:
    """Join style tags and drop duplicates. Hyphen and space count as the same tag.
    Psytrance is the short form of Psychedelic Trance. A shorter tag is dropped
    when a longer one already contains all of its words, so Techno/Peak Time/Driving
    collapse into Peak Time Techno/Driving Techno.
    """
    items = []
    seen = set()
    for chunk in chunks:
        for part in str(chunk or "").replace(";", "/").split("/"):
            label = part.strip()
            if not label:
                continue
            key = " ".join(label.casefold().replace("-", " ").replace("_", " ").split())
            short = SHORT_TAGS.get(key)
            if short:
                key = "psytrance"
                label = short
            if key in seen:
                continue
            seen.add(key)
            items.append((label, tuple(key.split())))
    kept = []
    for label, words in items:
        if any(set(words) < set(other) for _, other in items):
            continue
        kept.append(label)
    return "/".join(kept)


def write_subgenre_tag(filepath: str, subgenres: str) -> tuple[bool, Optional[str]]:
    """Write subgenres to the composer tag of an audio file."""
    subgenres = join_tags(subgenres)
    if not mutagen_ready():
        return False, "mutagen not available"

    try:
        p = Path(filepath)
        suffix = p.suffix.lower()

        if suffix == '.mp3':
            # Try multiple write strategies for MP3
            audio = None
            try:
                audio = MP3(str(p), ID3=EasyID3)
            except Exception:
                pass

            if not audio:
                try:
                    raw = MP3(str(p))
                    if not raw.tags:
                        raw.add_tags()
                    raw.save()
                    audio = MP3(str(p), ID3=EasyID3)
                except Exception:
                    pass

            if not audio:
                try:
                    from mutagen.id3 import ID3, TCOM
                    tags = ID3()
                    tags.add(TCOM(encoding=3, text=[subgenres]))
                    tags.save(str(p))
                    return True, None
                except Exception as e:
                    return False, str(e)

            if not audio:
                try:
                    tags = EasyID3(str(p)) if p.exists() else EasyID3()
                    tags['composer'] = subgenres
                    tags.save(str(p))
                    return True, None
                except Exception as e:
                    return False, str(e)

            if not audio:
                return False, "All MP3 write strategies failed"

        elif suffix == '.flac' and FLAC:
            try:
                audio = FLAC(str(p))
            except Exception:
                return False, "FLAC could not be opened"
        elif suffix in ('.m4a', '.aac', '.mp4') and M4A:
            try:
                audio = M4A(str(p))
            except Exception:
                return False, "M4A/AAC could not be opened"
            audio['\xa9wrt'] = subgenres
            audio.save()
            return True, None
        elif suffix == '.ogg' and OggVorbis:
            try:
                audio = OggVorbis(str(p))
            except Exception:
                return False, "OGG could not be opened"
        elif suffix == '.opus' and OggOpus:
            try:
                audio = OggOpus(str(p))
            except Exception:
                return False, "OPUS could not be opened"
        else:
            return False, "Unsupported format"

        if not audio:
            return False, "Could not create audio object"

        audio['composer'] = subgenres
        audio.save()
        return True, None

    except Exception as e:
        logger.error(f"write_subgenre_tag error for {filepath}: {e}")
        return False, str(e)


def write_year_tag(filepath: str, year: Any) -> tuple[bool, Optional[str]]:
    """Write the release year only when the date tag is empty."""
    year_str = str(year).strip() if year else ""
    if not year_str.isdigit() or not (1900 <= int(year_str) <= 2100):
        return False, f"invalid year: {year}"

    try:
        p = Path(filepath)
        suffix = p.suffix.lower()

        if suffix == '.mp3':
            from mutagen.id3 import ID3, ID3NoHeaderError, TDRC
            try:
                tags = ID3(str(p))
            except ID3NoHeaderError:
                tags = ID3()
            if tags.getall('TDRC') or tags.getall('TYER'):
                return False, "year already set"
            tags.add(TDRC(encoding=3, text=[year_str]))
            tags.save(str(p))
            return True, None

        if suffix == '.flac' and FLAC:
            audio = FLAC(str(p))
            if audio.get('date') and str(audio.get('date')[0]).strip():
                return False, "year already set"
            audio['date'] = year_str
            audio.save()
            return True, None

        if suffix in ('.m4a', '.aac', '.mp4') and M4A:
            audio = M4A(str(p))
            existing = audio.tags.get('\xa9day') if audio.tags else None
            if existing and str(existing[0]).strip():
                return False, "year already set"
            audio['\xa9day'] = year_str
            audio.save()
            return True, None

        if suffix == '.ogg' and OggVorbis:
            audio = OggVorbis(str(p))
        elif suffix == '.opus' and OggOpus:
            audio = OggOpus(str(p))
        else:
            return False, "unsupported format for year"

        if audio.get('date') and str(audio.get('date')[0]).strip():
            return False, "year already set"
        audio['date'] = year_str
        audio.save()
        return True, None

    except Exception as e:
        logger.error(f"write_year_tag error for {filepath}: {e}")
        return False, str(e)


def write_genre_tag(filepath: str, genre: str) -> tuple[bool, Optional[str]]:
    """Overwrite the genre tag with the given subgenre string."""
    try:
        p = Path(filepath)
        suffix = p.suffix.lower()

        if suffix == '.mp3':
            from mutagen.id3 import ID3, ID3NoHeaderError, TCON
            try:
                tags = ID3(str(p))
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall('TCON')
            tags.add(TCON(encoding=3, text=[genre]))
            tags.save(str(p))
            return True, None

        if suffix == '.flac' and FLAC:
            audio = FLAC(str(p))
        elif suffix in ('.m4a', '.aac', '.mp4') and M4A:
            audio = M4A(str(p))
            audio['\xa9gen'] = genre
            audio.save()
            return True, None
        elif suffix == '.ogg' and OggVorbis:
            audio = OggVorbis(str(p))
        elif suffix == '.opus' and OggOpus:
            audio = OggOpus(str(p))
        else:
            return False, "unsupported format"

        audio['genre'] = genre
        audio.save()
        return True, None

    except Exception as e:
        logger.error(f"write_genre_tag error for {filepath}: {e}")
        return False, str(e)


def read_extra_tags(filepath: str) -> tuple[str, str]:
    """User1 and album artist live in the file. VirtualDJ does not have to be running."""
    user1 = ""
    album = ""
    try:
        p = Path(filepath)
        suffix = p.suffix.lower()
        if suffix == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                raw = ID3(str(p))
            except ID3NoHeaderError:
                return "", ""
            frame = raw.get("TXXX:User1")
            if frame and frame.text:
                user1 = str(frame.text[0]).strip()
            frame = raw.get("TPE2")
            if frame and frame.text:
                album = str(frame.text[0]).strip()
        elif suffix == ".flac" and FLAC:
            audio = FLAC(str(p))
            user1 = str((audio.get("User1") or [""])[0] or "").strip()
            album = str((audio.get("albumartist") or [""])[0] or "").strip()
        elif suffix in (".ogg",) and OggVorbis:
            audio = OggVorbis(str(p))
            user1 = str((audio.get("User1") or [""])[0] or "").strip()
            album = str((audio.get("albumartist") or [""])[0] or "").strip()
        elif suffix == ".opus" and OggOpus:
            audio = OggOpus(str(p))
            user1 = str((audio.get("User1") or [""])[0] or "").strip()
            album = str((audio.get("albumartist") or [""])[0] or "").strip()
        elif suffix in (".m4a", ".aac", ".mp4") and M4A:
            audio = M4A(str(p))
            album = str((audio.get("aART") or [""])[0] or "").strip()
            raw = audio.get("----:com.apple.iTunes:User1")
            if raw:
                user1 = raw[0].decode("utf-8", "replace").strip()
    except Exception as exc:
        logger.warning(f"read_extra_tags {filepath}: {exc}")
    return user1, album


def write_user1_tag(filepath: str, text: str) -> tuple[bool, Optional[str]]:
    try:
        p = Path(filepath)
        suffix = p.suffix.lower()
        if suffix == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError, TXXX
            try:
                tags = ID3(str(p))
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("TXXX:User1")
            if text:
                tags.add(TXXX(encoding=3, desc="User1", text=[text]))
            tags.save(str(p))
            return True, None
        if suffix == ".flac" and FLAC:
            audio = FLAC(str(p))
        elif suffix == ".ogg" and OggVorbis:
            audio = OggVorbis(str(p))
        elif suffix == ".opus" and OggOpus:
            audio = OggOpus(str(p))
        elif suffix in (".m4a", ".aac", ".mp4") and M4A:
            audio = M4A(str(p))
            if text:
                audio["----:com.apple.iTunes:User1"] = [text.encode("utf-8")]
            else:
                audio.pop("----:com.apple.iTunes:User1", None)
            audio.save()
            return True, None
        else:
            return False, "unsupported format"
        if text:
            audio["User1"] = [text]
        else:
            audio.pop("User1", None)
        audio.save()
        return True, None
    except Exception as exc:
        logger.error(f"write_user1_tag {filepath}: {exc}")
        return False, str(exc)


def write_albumartist_tag(filepath: str, text: str) -> tuple[bool, Optional[str]]:
    try:
        p = Path(filepath)
        suffix = p.suffix.lower()
        if suffix == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError, TPE2
            try:
                tags = ID3(str(p))
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("TPE2")
            if text:
                tags.add(TPE2(encoding=3, text=[text]))
            tags.save(str(p))
            return True, None
        if suffix == ".flac" and FLAC:
            audio = FLAC(str(p))
            key = "albumartist"
        elif suffix == ".ogg" and OggVorbis:
            audio = OggVorbis(str(p))
            key = "albumartist"
        elif suffix == ".opus" and OggOpus:
            audio = OggOpus(str(p))
            key = "albumartist"
        elif suffix in (".m4a", ".aac", ".mp4") and M4A:
            audio = M4A(str(p))
            key = "aART"
        else:
            return False, "unsupported format"
        if text:
            audio[key] = [text]
        else:
            audio.pop(key, None)
        audio.save()
        return True, None
    except Exception as exc:
        logger.error(f"write_albumartist_tag {filepath}: {exc}")
        return False, str(exc)


def write_composer_tag(filepath: str, text: str) -> tuple[bool, Optional[str]]:
    return write_subgenre_tag(filepath, text)


# ── Analysis worker (runs in background thread) ────────────────────────

_analysis_lock = threading.Lock()
_is_analyzing = False
_job_mode = None
_job_model = None
_analysis_error: Optional[str] = None


def set_analyzing(flag: bool):
    global _is_analyzing, _analysis_error
    with _analysis_lock:
        _is_analyzing = flag
        if flag:
            _analysis_error = None


def active_mode() -> str:
    return _job_mode or load_config().get("analyze_mode") or "web"


def is_analyzing() -> bool:
    with _analysis_lock:
        return _is_analyzing


def get_analysis_error() -> Optional[str]:
    with _analysis_lock:
        return _analysis_error


def active_audio_model() -> str:
    return _job_model or load_config().get("audio_model") or "effnet"


def recognize_audio(filepath: str, model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Run the selected local audio model. Python 3.15 has no onnxruntime wheel."""
    script = PLUGIN_DIR / "audio_style.py"
    model = model or active_audio_model()
    if model not in ("effnet", "maest", "both"):
        model = "effnet"
    candidates = [
        ["py", "-3.13", str(script), filepath, model],
    ]
    last = "Python 3.13 mit onnxruntime fehlt"
    for cmd in candidates:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
            continue
        raw = (proc.stdout or "").strip()
        if not raw:
            last = (proc.stderr or "audio worker leer").strip()[:300]
            continue
        try:
            data = json.loads(raw.splitlines()[-1])
        except json.JSONDecodeError:
            last = raw[:300]
            continue
        if data.get("error"):
            last = data["error"]
            continue
        logger.info(f"AUDIO {Path(filepath).name} | {data.get('subgenres')} | {data.get('scores')}")
        return data
    logger.error(f"AUDIO failed {filepath}: {last}")
    return {"error": last}


_api_lock = threading.Lock()
_api_running: Dict[str, str] = {}


def _track_label(tags: Dict[str, Any], filepath: str) -> str:
    artist = (tags.get("artist") or "").strip()
    title = (tags.get("title") or "").strip()
    if artist and title:
        return f"{artist} - {title}"
    return Path(filepath).name


def try_api_slot(filepath: str, label: str):
    workers = max(1, int(load_config().get("workers") or 1))
    with _api_lock:
        if filepath in _api_running or len(_api_running) >= workers:
            return False, list(_api_running.values())
        _api_running[filepath] = label
        return True, []


def release_api_slot(filepath: str) -> None:
    with _api_lock:
        _api_running.pop(filepath, None)


def ensure_local_llm() -> Optional[str]:
    if llm_local._listening(llm_local.ANALYZE_PORT):
        return None
    try:
        llm_local.start_servers(summarize=False)
    except (FileNotFoundError, OSError) as exc:
        return str(exc)
    for _ in range(90):
        if llm_local._listening(llm_local.ANALYZE_PORT):
            return None
        time.sleep(1)
    return "Lokale LLM startet nicht. Runtime und 3B-Modell in den Einstellungen laden."


def analyze_single_track(filepath: str, dry_run: bool, force: bool = False, mode: Optional[str] = None, audio_model: Optional[str] = None) -> Dict[str, Any]:
    """Process a single track: read tags → send to Hermes → write result."""
    display = Path(filepath).name

    support_error = tag_support(filepath)
    if support_error:
        logger.info(f"Unsupported, not sent to Hermes: {filepath} | {support_error}")
        set_status(filepath, 'unsupported', error=support_error)
        return {'status': 'unsupported', 'display': display, 'reason': support_error}

    # Keygen check
    if is_keygen(filepath):
        logger.info(f"Keygen skipped: {filepath}")
        return {'status': 'keygen', 'display': display}

    # Read tags
    tags = read_tags(filepath)
    genre = tags.get('genre', '') or ''
    set_status(filepath, 'analyzing', genre=genre, artist=tags.get('artist', '') or '', title=tags.get('title', '') or '')
    composer_raw = tags.get('composer', '') or ''

    # Skip only when both composer and year are already written
    if not force and composer_raw.strip() and str(tags.get('date', '')).strip():
        logger.info(f"Skipped (composer and year set): {filepath}")
        set_status(filepath, 'skipped', genre=genre, subgenres=composer_raw.strip(), jahr=tags.get('date'))
        return {'status': 'skipped', 'display': display, 'reason': 'composer and year set'}

    artist = tags.get('artist', '') or '(no artist)'
    title = tags.get('title', '') or '(no title)'
    run_mode = mode or active_mode()
    logger.info(f"Analyzing: {filepath} | {run_mode}")
    held = False
    if run_mode == "audio":
        result = recognize_audio(filepath, audio_model)
    elif run_mode == "local":
        start_err = ensure_local_llm()
        if start_err:
            set_status(filepath, 'error', error=start_err, genre=genre)
            return {'status': 'error', 'display': display, 'reason': start_err}
        result = send_to_hermes(tags, mode="direct", backend="local")
    else:
        label = _track_label(tags, filepath)
        ok, running = try_api_slot(filepath, label)
        if not ok:
            return {'status': 'busy', 'display': display, 'running': running}
        held = True
        try:
            result = send_to_hermes(tags, mode="web" if run_mode in ("api", "web") else run_mode, backend="hermes")
        finally:
            release_api_slot(filepath)
            held = False

    if not result or 'subgenres' not in result:
        err_msg = (result or {}).get("error") or "Keine Subgenres erkannt"
        set_status(filepath, 'error', error=err_msg)
        logger.error(f"Error for {filepath}: {err_msg}")
        return {'status': 'error', 'display': display, 'reason': err_msg}

    subgenres = join_tags(result.get('subgenres', ''), result.get('einordnung') or '')
    if load_config().get("summarize") and active_mode() != "audio":
        subgenres = llm_local.shorten(subgenres)
    jahr = result.get('jahr')

    # Empty subgenres means Hermes could not verify anything — not an error.
    if not subgenres:
        set_status(filepath, 'unverified', genre=genre, jahr=jahr or 0)
        logger.info(f"No verified subgenres: {filepath}")
        return {'status': 'unverified', 'display': display, 'reason': 'no verified subgenres'}

    # Write to file
    if not dry_run:
        success, write_err = write_subgenre_tag(filepath, subgenres)
        year_note = ""
        if success and jahr and not str(tags.get('date', '')).strip():
            year_ok, year_err = write_year_tag(filepath, jahr)
            year_note = f" | Jahr {jahr}" if year_ok else f" | Jahr nicht geschrieben: {year_err}"
        extra_note = ""
        extra_value = ""
        extra_field = load_config().get("extra_tag") or "off"
        if success and run_mode == "audio" and result.get("user1") and extra_field != "off":
            extra_value = result.get("user1") or ""
            if extra_field == "albumartist":
                extra_ok, extra_err = write_albumartist_tag(filepath, extra_value)
            else:
                extra_ok, extra_err = write_user1_tag(filepath, extra_value)
                if not extra_ok:
                    extra_ok, extra_err = write_albumartist_tag(filepath, extra_value)
                    extra_field = "albumartist" if extra_ok else extra_field
                    extra_note = " | User1 nicht schreibbar, Album Artist benutzt" if extra_ok else f" | Extra nicht geschrieben: {extra_err}"
            if extra_ok and not extra_note:
                extra_note = f" | {extra_field} {extra_value}"
        if success:
            set_status(filepath, 'success', subgenres=subgenres, jahr=jahr or 0)
            logger.info(f"Success: {filepath} → {subgenres}{year_note}{extra_note}")
            return {'status': 'success', 'display': display, 'subgenres': subgenres, 'jahr': jahr, 'extra': extra_value, 'extra_field': extra_field}
        else:
            set_status(filepath, 'error', error=write_err or "Write failed")
            logger.error(f"Write error for {filepath}: {write_err}")
            return {'status': 'write_error', 'display': display, 'reason': write_err}
    else:
        # Dry run — just record result without writing file
        set_status(filepath, 'success', subgenres=subgenres, jahr=jahr or 0)
        logger.info(f"Dry-run success: {filepath} → {subgenres}")
        return {'status': 'success', 'display': display, 'subgenres': subgenres, 'jahr': jahr}


def update_from_github() -> Dict[str, Any]:
    repo = "kulimuh666/Kulimuh666s-Media-subgenre-analyzer"
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"User-Agent": "Kulimuh666-Media-Subgenre-Analyzer", "Accept": "application/vnd.github+json"}
    try:
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    tag = str(release.get("tag_name") or "")
    remote = int(re.sub(r"\D", "", tag) or "0")
    local_text = (PLUGIN_DIR / "VERSION").read_text(encoding="utf-8").strip() if (PLUGIN_DIR / "VERSION").is_file() else "0"
    local = int(re.sub(r"\D", "", local_text) or "0")
    if remote <= local:
        shown = f"V.{local:04d}"
        return {"ok": True, "current": True, "version": shown, "message": f"Schon die neueste Version {shown}."}
    zip_url = ""
    prefix = f"https://github.com/{repo}/releases/download/"
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name.lower().endswith(".zip") and url.startswith(prefix):
            zip_url = url
            break
    if not zip_url:
        return {"ok": False, "error": "Kein Zip in der neuesten Version."}
    try:
        req = urllib.request.Request(zip_url, headers={"User-Agent": "Kulimuh666-Media-Subgenre-Analyzer"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            blob = resp.read()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    skip_names = {"config.json", "done.json", "state.json", "analyze.log"}
    skip_dirs = {"models", "runtime", "release", ".git", "__pycache__", "_GENRE fehlt"}
    written = []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = Path(info.filename)
                if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                    return {"ok": False, "error": "Zip enthält einen unsicheren Pfad."}
                if rel.parts[0] in skip_dirs or rel.name in skip_names:
                    continue
                if rel.suffix.lower() in {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".zip"}:
                    continue
                target = PLUGIN_DIR.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
                written.append(rel.as_posix())
    except zipfile.BadZipFile:
        return {"ok": False, "error": "Zip ist beschädigt."}
    return {
        "ok": True,
        "current": False,
        "version": tag,
        "files": len(written),
        "message": f"Aktualisiert auf {tag}. Server-Fenster schließen und start_gui.bat neu starten. config.json und Modelle bleiben.",
    }


def probe_hermes(url: str, key: str) -> Dict[str, Any]:
    if not url or not key:
        return {"ok": False, "error": "Adresse oder Key fehlt"}
    payload = json.dumps({
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
        "max_tokens": 16,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(4000).decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read(400).decode("utf-8", "replace")
        return {"ok": False, "error": f"HTTP {exc.code}: {body[:300]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    detail = ""
    try:
        parsed = json.loads(raw)
        detail = str(((parsed.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()[:200]
    except json.JSONDecodeError:
        detail = raw[:200]
    return {"ok": True, "status": status, "detail": detail or "Antwort ohne Text"}


def file_has_genre(filepath: str) -> bool:
    tags = read_tags(filepath)
    return bool((tags.get('genre') or '').strip())


def file_is_filled(filepath: str) -> bool:
    tags = read_tags(filepath)
    return bool((tags.get('genre') or '').strip() or (tags.get('composer') or '').strip())


def load_done() -> set:
    path = PLUGIN_DIR / "done.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(item) for item in data if item}


def save_done(paths: set) -> None:
    path = PLUGIN_DIR / "done.json"
    path.write_text(json.dumps(sorted(paths), ensure_ascii=False, indent=2), encoding="utf-8")


def run_analysis(target_dir: str, dry_run: bool = False, limit: Optional[int] = None, hide_done: bool = False, files: Optional[List[str]] = None):
    """Run analysis on all tracks in target_dir (called from background thread)."""
    global _state_data

    set_analyzing(True)
    logger.info(f"Analysis started for {target_dir}")

    # Discover files
    if files:
        audio_files = [fp for fp in files if Path(fp).is_file()]
    else:
        audio_files = find_audio_files(target_dir)

    if not audio_files:
        set_status("dir", 'error', error=f"No audio files found in {target_dir}")
        save_state()
        set_analyzing(False)
        logger.info(f"No audio files found in {target_dir}")
        return

    if not files and hide_done:
        done = load_done()
        audio_files = [fp for fp in audio_files if fp not in done and not file_is_filled(fp)]

    # Apply limit after finished files are excluded, so the cap is not filled by them.
    if not files and limit and limit > 0:
        audio_files = audio_files[:limit]

    total = len(audio_files)
    logger.info(f"Found {total} audio files")

    # Register only this job. A leftover list from the last run must not come back in the status poll.
    with _state_lock:
        _state_data["tracks"] = []
        _state_data["total_files"] = len(audio_files)
        _state_data["completed"] = 0
    for fp in audio_files:
        add_track(fp)
        tags = read_tags(fp)
        update_track(
            fp,
            genre=tags.get('genre') or '',
            artist=tags.get('artist') or '',
            title=tags.get('title') or '',
            jahr=tags.get('date') or 0,
        )

    save_state()

    cfg = load_config()
    workers = max(1, int(cfg.get("workers", 1)))

    completed = 0
    analyzed = 0
    skipped = 0
    keygen_skipped = 0
    errors = 0

    # Process sequentially (API only handles 1 concurrent request)
    for i, filepath in enumerate(audio_files):
        if not is_analyzing():
            logger.info(f"Analysis cancelled at {i}/{total}")
            break

        result = analyze_single_track(filepath, dry_run)
        completed += 1

        status = result.get('status', 'unknown')
        if status == 'keygen':
            keygen_skipped += 1
        elif status in ('skipped',):
            skipped += 1
        elif status == 'success':
            analyzed += 1
        else:
            errors += 1

        # Progress update every track, save state
        if completed % max(1, workers) == 0 or completed >= total:
            _state_data["total_files"] = total
            _state_data["completed"] = analyzed
            save_state()

        logger.info(f"Progress: {completed}/{total} (+{analyzed}, -{skipped}, keygen:{keygen_skipped}, err:{errors})")

    # Final state save
    _state_data["total_files"] = total
    _state_data["completed"] = analyzed
    save_state()
    set_analyzing(False)
    global _job_mode, _job_model
    _job_mode = None
    _job_model = None

    logger.info(
        f"Analysis complete: {analyzed} success, {skipped} skipped, "
        f"{keygen_skipped} keygen, {errors} errors out of {total}"
    )


# ── HTTP Request Handler ────────────────────────────────────────────────

def reveal_in_explorer(filepath: str) -> None:
    """Select a file in Explorer. /select breaks on spaces and commas."""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.ILFindLastID.argtypes = [ctypes.c_void_p]
    shell32.ILFindLastID.restype = ctypes.c_void_p
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong,
    ]
    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long

    target = str(Path(filepath).resolve())
    folder = str(Path(target).parent)
    ole32.CoInitialize(None)
    pidl_folder = shell32.ILCreateFromPathW(folder)
    pidl_file = shell32.ILCreateFromPathW(target)
    if not pidl_folder or not pidl_file:
        raise OSError(f"shell could not parse {target}")
    try:
        relative = shell32.ILFindLastID(pidl_file)
        children = (ctypes.c_void_p * 1)(relative)
        hr = shell32.SHOpenFolderAndSelectItems(pidl_folder, 1, children, 0)
        if hr < 0:
            raise OSError(f"SHOpenFolderAndSelectItems failed: {hr}")
        _foreground_explorer(Path(target).parent.name)
    finally:
        shell32.ILFree(pidl_folder)
        shell32.ILFree(pidl_file)


def _foreground_explorer(folder_name: str) -> None:
    """Bring the Explorer window for this folder in front of the browser."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]

    time.sleep(0.3)
    found: List[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "CabinetWClass":
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if folder_name.lower() in title.value.lower():
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        return
    hwnd = found[-1]
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_SHOWWINDOW = 0x0040
    user32.ShowWindow(hwnd, 9)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    kernel32.GetCurrentThreadId()


class VDJHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the VDJ Analyzer API."""

    def log_message(self, format, *args):
        """Override to suppress default stderr logging."""
        pass  # We log via our own logger instead

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _read_body(self) -> bytes:
        length = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(length) if length > 0 else b''

    def _parse_json_body(self) -> Optional[Dict[str, Any]]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode('utf-8'))
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            self._send_json({"error": f"Invalid JSON: {e}"}, 400)
            return None

    # ── GET routes ────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')
        params = parse_qs(parsed.query)

        logger.info(f"GET {self.path}")

        # GUI-endpoints
        if path == '/health':
            self._handle_health()
        elif path == '/api/config':
            self._handle_get_config()
        elif path == '/api/files':
            self._handle_get_files(params)
        elif path == '/api/log':
            self._handle_get_log(params)
        elif path == '/api/status':
            self._handle_get_status()
        elif path == '/api/llm':
            self._send_json(llm_local.status())
        elif path == '/api/audio':
            self._handle_get_audio(params)
        else:
            self._send_json({"error": "Not found", "endpoints": [
                "GET /health",
                "POST /scan", "POST /analyze",
                "GET /api/config", "POST /api/config",
                "GET /api/files?dir=X",
                "GET /api/log", "GET /api/status"
            ]}, 404)

    def _handle_get_config(self):
        cfg = load_config()
        self._send_json(cfg)

    def _handle_get_files(self, params: Dict[str, List[str]]):
        target_dir = params.get('dir', [None])[0]
        if not target_dir:
            # Use config's target_dir as default
            cfg = load_config()
            target_dir = cfg.get("target_dir") or ""

        root = Path(target_dir)
        if not root.exists():
            self._send_json({
                "error": f"Ordner nicht gefunden: {target_dir}",
                "dir": target_dir,
                "count": 0,
                "shown": 0,
                "files": [],
            }, 404)
            return

        files = list_audio_paths(target_dir)
        try:
            cap = int(params.get('limit', ['0'])[0] or 0)
        except ValueError:
            cap = 0
        hide_done = params.get('hide_done', ['0'])[0] in ('1', 'true')
        done = load_done() if hide_done else set()

        after = params.get('after', [''])[0] or ''
        empty_only = params.get('empty_only', ['0'])[0] in ('1', 'true')
        passed_after = not after

        shown = []
        complete = 0
        for fp in files:
            if not passed_after:
                if fp == after:
                    passed_after = True
                continue
            if cap and len(shown) >= cap:
                break
            tags = read_tags(fp)
            composer = (tags.get('composer') or '').strip()
            year = (tags.get('date') or '').strip()
            genre = (tags.get('genre') or '').strip()
            if genre:
                complete += 1
            if hide_done and (fp in done or genre or composer):
                continue
            if empty_only and (genre or composer):
                continue
            support_error = tag_support(fp)
            if support_error:
                shown.append({
                    "filepath": fp,
                    "filename": Path(fp).name,
                    "genre": tags.get('genre', ''),
                    "artist": tags.get('artist', '') or '',
                    "title": tags.get('title', '') or '',
                    "composer": composer,
                    "user1": tags.get("user1") or "",
                    "albumartist": tags.get("albumartist") or "",
                    "jahr": year,
                    "status": "unsupported",
                    "error": support_error,
                })
                if cap and len(shown) >= cap:
                    break
                continue
            shown.append({
                "filepath": fp,
                "filename": Path(fp).name,
                "genre": tags.get('genre', ''),
                "artist": tags.get('artist', '') or '',
                "title": tags.get('title', '') or '',
                "composer": composer,
                "user1": tags.get("user1") or "",
                "albumartist": tags.get("albumartist") or "",
                "jahr": year,
                "status": "skipped" if (composer and year) else "pending",
            })
        self._send_json({
            "dir": target_dir,
            "count": len(files),
            "shown": len(shown),
            "complete": complete,
            "files": shown,
        })

    def _handle_get_log(self, params: Dict[str, List[str]]):
        lines = int(params.get('lines', ['100'])[0])
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
            self._send_json({"log": ''.join(tail), "total_lines": len(all_lines)})
        except FileNotFoundError:
            self._send_json({"log": "", "total_lines": 0})

    def _handle_get_status(self):
        state = get_state_copy()
        tracks = state.get("tracks", [])

        success_count = sum(1 for t in tracks if t.get("status") == "success")
        error_count = sum(1 for t in tracks if t.get("status") == "error")
        skipped_count = sum(1 for t in tracks if t.get("status") == "skipped")
        keygen_count = sum(1 for t in tracks if t.get("status") == "keygen")
        pending_count = sum(1 for t in tracks if t.get("status") == "pending")
        analyzing_count = sum(1 for t in tracks if t.get("status") == "analyzing")

        # Resume info from previous state
        resume_info = None
        if STATE_PATH.exists() and STATE_PATH.stat().st_size > 0:
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    prev_state = json.load(f)
                prev_tracks = prev_state.get("tracks", [])
                prev_success = sum(1 for t in prev_tracks if t.get("status") == "success")
                prev_pending = sum(1 for t in prev_tracks if t.get("status") == "pending")
                if prev_success > 0 or prev_pending > 0:
                    resume_info = {
                        "has_previous": True,
                        "previous_completed": prev_success,
                        "previous_pending": prev_pending,
                        "last_update": prev_state.get("last_update", ""),
                    }
            except Exception:
                pass

        self._send_json({
            "is_analyzing": is_analyzing(),
            "mutagen": EasyID3 is not None,
            "python": sys.executable,
            "analysis_error": get_analysis_error(),
            "state": {
                "total_files": state.get("total_files", 0),
                "completed": success_count,
                "success": success_count,
                "error": error_count,
                "skipped": skipped_count,
                "keygen_skipped": keygen_count,
                "pending": pending_count,
                "analyzing": analyzing_count,
            },
            "resume_info": resume_info,
            "tracks": [
                {
                    "filepath": t.get("filepath", ""),
                    "filename": Path(t.get("filepath", "")).name,
                    "status": t.get("status", ""),
                    "subgenres": t.get("subgenres", ""),
                    "genre": t.get("genre", ""),
                    "artist": t.get("artist", ""),
                    "title": t.get("title", ""),
                    "jahr": t.get("jahr", 0),
                    "error": t.get("error", ""),
                }
                for t in tracks
            ],
        })

    # ── POST routes ───────────────────────────────────────────────────

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/')

        logger.info(f"POST {self.path}")

        if path == '/api/config':
            self._handle_post_config()
        elif path == '/api/analyze':
            self._handle_post_analyze()
        elif path == '/api/stop':
            set_analyzing(False)
            global _job_mode, _job_model
            _job_mode = None
            _job_model = None
            logger.info("Analysis stop requested")
            self._send_json({"message": "stop requested"})
        elif path == '/api/apply-genre':
            self._handle_apply_genre()
        elif path == '/api/apply-field':
            self._handle_apply_field()
        elif path == '/api/update':
            self._handle_update()
        elif path == '/api/test-api':
            self._handle_test_api()
        elif path == '/api/done':
            self._handle_done()
        elif path == '/api/reveal':
            self._handle_reveal()
        elif path == '/api/row':
            self._handle_row()
        elif path == '/api/llm/download':
            self._handle_llm_download()
        elif path == '/api/llm/start':
            self._handle_llm_start()
        else:
            self._send_json({"error": "Not found"}, 404)

    def _handle_get_audio(self, params: Dict[str, List[str]]):
        """Stream an audio file so the GUI can preview it."""
        fp = params.get('path', [None])[0]
        if not fp or not Path(fp).is_file():
            self._send_json({"error": "file not found"}, 404)
            return
        if Path(fp).suffix.lower() not in SUPPORTED_EXTENSIONS:
            self._send_json({"error": "unsupported format"}, 415)
            return

        mime = {
            '.mp3': 'audio/mpeg', '.flac': 'audio/flac',
            '.m4a': 'audio/mp4', '.mp4': 'audio/mp4', '.aac': 'audio/aac',
            '.ogg': 'audio/ogg', '.opus': 'audio/ogg',
        }[Path(fp).suffix.lower()]
        size = Path(fp).stat().st_size
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get('Range')
        if range_header and range_header.startswith('bytes='):
            spec = range_header.split('=', 1)[1].split('-')
            start = int(spec[0]) if spec[0] else 0
            end = int(spec[1]) if len(spec) > 1 and spec[1] else size - 1
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            with open(fp, 'rb') as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _handle_llm_download(self):
        body = self._parse_json_body()
        if body is None:
            return
        which = body.get("which")
        try:
            started = llm_local.start_download(which)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        self._send_json({"which": which, "started": started})

    def _handle_llm_start(self):
        cfg = load_config()
        try:
            started = llm_local.start_servers(bool(cfg.get("summarize")))
        except (FileNotFoundError, OSError) as exc:
            logger.error(f"LLM start failed: {exc}")
            self._send_json({"error": str(exc)}, 400)
            return
        self._send_json({"started": started})

    def _handle_apply_genre(self):
        """Copy the given subgenre string into the file's genre tag."""
        body = self._parse_json_body()
        if body is None:
            return
        fp = body.get('filepath', '')
        genre = (body.get('genre') or '').strip()
        if not fp or not Path(fp).is_file():
            self._send_json({"error": "file not found"}, 404)
            return
        if not genre and not body.get('keep_status'):
            self._send_json({"error": "no genre given"}, 400)
            return
        ok, err = write_genre_tag(fp, genre)
        if not ok:
            self._send_json({"error": err or "write failed"}, 500)
            return
        if body.get('keep_status'):
            update_track(fp, genre=genre)
        else:
            set_status(fp, 'success', genre=genre)
        logger.info(f"Genre applied: {fp} → {genre}")
        self._send_json({"filepath": fp, "genre": genre})

    def _handle_apply_field(self):
        body = self._parse_json_body()
        if body is None:
            return
        fp = body.get("filepath") or ""
        field = body.get("field") or ""
        value = (body.get("value") if body.get("value") is not None else "").strip()
        writers = {
            "genre": write_genre_tag,
            "composer": write_composer_tag,
            "user1": write_user1_tag,
            "albumartist": write_albumartist_tag,
        }
        if field not in writers:
            self._send_json({"error": "unknown field"}, 400)
            return
        if not fp or not Path(fp).is_file():
            self._send_json({"error": "file not found"}, 404)
            return
        ok, err = writers[field](fp, value)
        if not ok:
            self._send_json({"error": err or "write failed"}, 500)
            return
        logger.info(f"Field {field}: {fp} → {value}")
        self._send_json({"filepath": fp, "field": field, "value": value})

    def _handle_row(self):
        body = self._parse_json_body()
        if body is None:
            return
        fp = body.get("filepath") or ""
        mode = body.get("mode") or "api"
        model = body.get("model") if body.get("model") in ("effnet", "maest", "both") else None
        if mode in ("effnet", "maest", "both"):
            model = mode
            mode = "audio"
        elif mode not in ("api", "local", "audio"):
            mode = "api"
        if not fp or not Path(fp).is_file():
            self._send_json({"error": "file not found"}, 404)
            return
        result = analyze_single_track(fp, False, force=True, mode=mode, audio_model=model)
        if result.get("status") == "busy":
            self._send_json({"busy": True, "running": result.get("running") or []}, 409)
            return
        code = 200 if result.get("status") == "success" else 500
        self._send_json(result, code)

    def _handle_update(self):
        result = update_from_github()
        logger.info(f"Update {result.get('version') or ''} ok={result.get('ok')} {result.get('error') or result.get('message') or ''}")
        self._send_json(result, 200 if result.get("ok") else 400)

    def _handle_test_api(self):
        body = self._parse_json_body()
        if body is None:
            return
        cfg = load_config()
        url = (body.get("hermes_url") or cfg.get("hermes_url") or "").strip()
        key = (body.get("api_key") or cfg.get("api_key") or "").strip()
        result = probe_hermes(url, key)
        logger.info(f"API test {url} ok={result.get('ok')} {result.get('error') or result.get('detail') or ''}")
        self._send_json(result, 200 if result.get("ok") else 400)

    def _handle_done(self):
        body = self._parse_json_body()
        if body is None:
            return
        fp = body.get("filepath") or ""
        if not fp:
            self._send_json({"error": "filepath missing"}, 400)
            return
        done = load_done()
        done.add(fp)
        save_done(done)
        self._send_json({"ok": True, "filepath": fp})

    def _handle_reveal(self):
        """Open Explorer with this file selected."""
        body = self._parse_json_body()
        if body is None:
            return
        fp = body.get("filepath", "")
        path = Path(fp)
        if not fp or not path.exists():
            self._send_json({"error": "file not found"}, 404)
            return
        target = str(path.resolve())
        try:
            reveal_in_explorer(target)
        except OSError as exc:
            logger.error(f"Reveal failed for {target}: {exc}")
            self._send_json({"error": str(exc)}, 500)
            return
        logger.info(f"Reveal: {target}")
        self._send_json({"filepath": target})

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()

    def _handle_post_config(self):
        body = self._parse_json_body()
        if body is None:
            return

        cfg = load_config()

        # Validate and update fields
        valid_keys = set(DEFAULT_CONFIG.keys())
        for key in body:
            if key not in valid_keys:
                logger.warning(f"Unknown config key: {key}")

        for key, value in body.items():
            if key == "workers":
                cfg["workers"] = max(1, int(value))
            elif key == "dry_run":
                cfg["dry_run"] = bool(value)
            elif key == "limit":
                cfg["limit"] = int(value) if value is not None else None
            elif key == "timeout":
                cfg["timeout"] = int(value)
            elif key == "analyze_mode":
                cfg["analyze_mode"] = value if value in ("web", "direct", "audio") else "web"
            elif key == "audio_model":
                cfg["audio_model"] = value if value in ("effnet", "maest", "both") else "effnet"
            elif key == "extra_tag":
                cfg["extra_tag"] = value if value in ("user1", "albumartist", "off") else "user1"
            elif key == "llm_backend":
                cfg["llm_backend"] = value if value in ("hermes", "local") else "hermes"
            elif key == "summarize":
                cfg["summarize"] = bool(value)
            else:
                cfg[key] = value

        save_config(cfg)
        logger.info(f"Config updated: {json.dumps(body, ensure_ascii=False)}")
        self._send_json({"config": cfg, "message": "Config saved"})

    def _handle_post_analyze(self):
        global _state_data

        body = self._parse_json_body()
        if body is None:
            return

        # Check if already analyzing
        if is_analyzing():
            self._send_json({"error": "Analysis already running"}, 409)
            return

        target_dir = body.get("dir", "")
        dry_run = bool(body.get("dry_run", False))
        limit = body.get("limit")
        resume = bool(body.get("resume", True))

        if not target_dir:
            cfg = load_config()
            target_dir = cfg.get("target_dir") or ""

        target_dir = str(Path(target_dir))

        # Validate directory exists on THIS machine
        if not Path(target_dir).exists():
            self._send_json({
                "error": f"Directory does not exist on the server PC: {target_dir}. The server only sees drives on the PC it runs on."
            }, 400)
            return

        # Start analysis in background thread
        hide_done = bool(body.get("hide_done", False))
        picked = body.get("files") if isinstance(body.get("files"), list) else None
        forced = body.get("mode")
        forced_model = body.get("audio_model")
        global _job_mode, _job_model
        _job_mode = forced if forced in ("web", "direct", "audio") else None
        _job_model = forced_model if forced_model in ("effnet", "maest", "both") else None
        t = threading.Thread(
            target=run_analysis,
            args=(target_dir, dry_run, limit, hide_done, picked),
            daemon=True,
        )
        t.start()

        self._send_json({
            "message": f"Analysis started for {target_dir}",
            "dir": target_dir,
            "dry_run": dry_run,
            "limit": limit,
        })


# ── Main / Entry Point ────────────────────────────────────────────────

def main():
    """Start the VDJ Analyzer HTTP server."""
    print("=" * 60)
    print("Kulimuh666's Media Subgenre Analyzer Server V.0032")
    print("=" * 60)

    # Start auto-save timer
    start_auto_save()
    logger.info("Auto-save started (every 30s)")

    server = ThreadingHTTPServer(('0.0.0.0', SERVER_PORT), VDJHandler)
    print(f"\n🚀 Server running on http://0.0.0.0:{SERVER_PORT}")
    print(f"   Endpoints:")
    print(f"     GET  /api/config          - Read configuration")
    print(f"     POST /api/config          - Update configuration")
    print(f"     GET  /api/files?dir=X     - List audio files")
    print(f"     POST /api/analyze         - Start analysis")
    print(f"     GET  /api/log             - Read log file")
    print(f"     GET  /api/status          - Status & progress")

    print("\nPress Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        stop_auto_save()
        save_state()  # Final save
        logger.info("Server stopped by user")
        server.server_close()
        print("Goodbye.")


if __name__ == '__main__':
    main()
