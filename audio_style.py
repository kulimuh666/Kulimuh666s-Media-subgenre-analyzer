"""Recognize Discogs styles from the audio itself. Not from tags.

Uses the MTG Discogs-EffNet model (400 styles) via ONNX. Python 3.15 has no
onnxruntime wheel, so the 3.15 server calls this with Python 3.13.
"""
import json
import sys
from pathlib import Path

import av
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parent
MODEL = ROOT / "models" / "discogs-effnet-bsdynamic-1.onnx"
LABELS = ROOT / "models" / "discogs-effnet-bs64-1.json"
SR = 16000
FRAME = 512
HOP = 256
BANDS = 96
PATCH = 128
PATCH_HOP = 62
SECONDS = 30


def _hz_to_mel(hz):
    return 2595.0 * np.log10(1.0 + hz / 700.0) if False else 3.0 * np.log(1.0 + hz / 200.0) / np.log(1.0 + 1000.0 / 200.0)


def _mel_to_hz(mel):
    return 200.0 * (np.exp(mel * np.log(1.0 + 1000.0 / 200.0) / 3.0) - 1.0)


def _filterbank():
    n_freq = FRAME // 2 + 1
    low_m = _hz_to_mel(0.0)
    high_m = _hz_to_mel(SR / 2)
    step = (high_m - low_m) / (BANDS + 1)
    hz = _mel_to_hz(low_m + step * np.arange(BANDS + 2))
    scale = (SR / 2) / (n_freq - 1)
    fb = np.zeros((BANDS, n_freq), dtype=np.float32)
    for i in range(BANDS):
        left, center, right = float(hz[i]), float(hz[i + 1]), float(hz[i + 2])
        fstep1 = center - left
        fstep2 = right - center
        jbegin = int(np.ceil(left / scale))
        jend = int(np.floor(right / scale))
        jend = min(jend, n_freq - 1)
        for j in range(max(0, jbegin), jend + 1):
            binfreq = j * scale
            if binfreq < center:
                fb[i, j] = (binfreq - left) / fstep1 if fstep1 else 0
            else:
                fb[i, j] = (right - binfreq) / fstep2 if fstep2 else 0
        area = (fstep1 + fstep2) / 2.0
        if area:
            fb[i] /= area
    return fb


FB = _filterbank()
WINDOW = (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(FRAME) / (FRAME - 1))).astype(np.float32)


def load_audio(path):
    container = av.open(path)
    stream = container.streams.audio[0]
    chunks = []
    resampler = av.audio.resampler.AudioResampler(format="fltp", layout="mono", rate=SR)
    for frame in container.decode(stream):
        for out in resampler.resample(frame):
            chunks.append(out.to_ndarray().reshape(-1))
    for out in resampler.resample(None):
        chunks.append(out.to_ndarray().reshape(-1))
    container.close()
    if not chunks:
        raise RuntimeError("no audio")
    audio = np.concatenate(chunks).astype(np.float32)
    return audio


def segments(audio):
    """The whole file. Three windows missed the drop and kept the breakdown."""
    return [audio]


def mels(audio):
    padded = np.concatenate([np.zeros(FRAME // 2, dtype=np.float32), np.asarray(audio, dtype=np.float32)])
    n = 1 + max(0, (padded.size - FRAME) // HOP)
    if n < 1:
        padded = np.zeros(FRAME, dtype=np.float32)
        n = 1
    frames = np.lib.stride_tricks.as_strided(
        padded,
        shape=(n, FRAME),
        strides=(padded.strides[0] * HOP, padded.strides[0]),
        writeable=False,
    ).copy()
    spec = np.abs(np.fft.rfft(frames * WINDOW, axis=1))
    return np.log10(1.0 + 10000.0 * (spec @ FB.T)).astype(np.float32)


def patches(bands):
    if bands.shape[0] < PATCH:
        pad = np.zeros((PATCH - bands.shape[0], BANDS), dtype=np.float32)
        return np.expand_dims(np.vstack([bands, pad]), 0)
    out = []
    i = 0
    while i + PATCH <= bands.shape[0]:
        out.append(bands[i:i + PATCH])
        i += PATCH_HOP
    return np.stack(out)


MAEST = ROOT / "models" / "discogs-maest-30s-pw-519l-2.onnx"
MAEST_LABELS = ROOT / "models" / "discogs-maest-30s-pw-519l-2.json"
MTT = ROOT / "models" / "mtt-musicnn-1.onnx"
MTT_LABELS = ROOT / "models" / "mtt-musicnn-1.json"
MAEST_PATCH = 1876
MAEST_HOP = 1875
MAEST_MEAN = 2.06755686098554
MAEST_STD = 1.268292820667291
MTT_PATCH = 187
MTT_HOP = 187
VAGUE = {"Noise", "Experimental", "Field Recording", "Abstract", "Musique Concrète", "Drone", "Sound Collage"}
_sessions = {}


def session(path):
    key = str(path)
    if key not in _sessions:
        _sessions[key] = ort.InferenceSession(key, providers=["CPUExecutionProvider"])
    return _sessions[key]


def style_name(label):
    name = label.split("---")[-1].strip()
    if name.lower() in ("psy-trance", "psychedelic trance"):
        return "Psytrance"
    return name


def keep_styles(ranked):
    specific = [name for score, name in ranked if score >= 0.03 and name not in VAGUE]
    pool = specific or [name for score, name in ranked if score >= 0.03]
    kept = []
    for name in pool:
        if len(kept) >= 12:
            break
        if name not in kept:
            kept.append(name)
    if not kept and ranked:
        kept = [ranked[0][1]]
    return kept


def score_model(path, labels_path, feed_batches, output_name=None):
    classes = json.loads(labels_path.read_text(encoding="utf-8"))["classes"]
    sess = session(path)
    inp = sess.get_inputs()[0]
    total = None
    count = 0
    for feed in feed_batches:
        if len(inp.shape) == 4 or (inp.shape and inp.shape[1] == 1):
            feed = feed[:, None, :, :]
        raw = sess.run([output_name] if output_name else None, {inp.name: feed})[0]
        flat = raw.reshape(-1, raw.shape[-1]).sum(axis=0)
        total = flat if total is None else total + flat
        count += raw.reshape(-1, raw.shape[-1]).shape[0]
    scores = total / max(count, 1)
    return sorted(
        ((float(scores[i]), style_name(classes[i])) for i in range(min(len(classes), scores.size))),
        reverse=True,
    )


def effnet_ranked(bands):
    return score_model(MODEL, LABELS, [patches(bands)])


def maest_ranked(bands):
    classes = json.loads(MAEST_LABELS.read_text(encoding="utf-8"))["classes"]
    sess = session(MAEST)
    inp = sess.get_inputs()[0]
    starts = list(range(0, max(0, bands.shape[0] - MAEST_PATCH + 1), MAEST_HOP))
    if not starts:
        pad = np.full((MAEST_PATCH, bands.shape[1]), MAEST_MEAN, dtype=np.float32)
        pad[:bands.shape[0]] = bands
        chunks = [pad]
    else:
        chunks = [bands[start:start + MAEST_PATCH] for start in starts]
    total = None
    for chunk in chunks:
        feed = ((chunk - MAEST_MEAN) / (MAEST_STD * 2)).astype(np.float32)[None, :, :]
        raw = sess.run(["activations"], {inp.name: feed})[0]
        flat = raw.reshape(-1, raw.shape[-1])[0]
        total = flat if total is None else total + flat
    scores = total / len(chunks)
    return sorted(
        ((float(scores[i]), style_name(classes[i])) for i in range(min(len(classes), scores.size))),
        reverse=True,
    )


def merge_ranked(*groups):
    best = {}
    for ranked in groups:
        for score, name in ranked:
            best[name] = max(best.get(name, 0.0), score)
    return sorted(((score, name) for name, score in best.items()), reverse=True)


def mtt_tags(bands):
    classes = json.loads(MTT_LABELS.read_text(encoding="utf-8"))["classes"]
    style = {
        "techno", "electronic", "electric", "rock", "pop", "dance", "metal",
        "ambient", "classical", "classic", "new age", "country", "indian",
        "opera", "slow", "fast", "loud", "quiet", "soft", "weird", "beat", "beats",
    }
    chunks = []
    if bands.shape[0] < 187:
        pad = np.zeros((187, bands.shape[1]), dtype=np.float32)
        pad[:bands.shape[0]] = bands
        chunks = [pad]
    else:
        step = 187
        for start in range(0, bands.shape[0] - 186, step):
            chunks.append(bands[start:start + 187])
            if len(chunks) >= 8:
                break
    sess = session(MTT)
    inp = sess.get_inputs()[0]
    names = [o.name for o in sess.get_outputs()]
    out_name = "model/Sigmoid" if "model/Sigmoid" in names else ("activations" if "activations" in names else names[0])
    total = None
    for chunk in chunks:
        feed = chunk.astype(np.float32)[None, :, :]
        if len(inp.shape) == 4 or (inp.shape and inp.shape[1] == 1):
            feed = feed[:, None, :, :]
        raw = sess.run([out_name], {inp.name: feed})[0]
        flat = raw.reshape(-1)[:len(classes)]
        total = flat if total is None else total + flat
    scores = total / max(len(chunks), 1)
    ranked = sorted(
        ((float(scores[i]), style_name(classes[i])) for i in range(min(len(classes), scores.size))),
        reverse=True,
    )
    if len(ranked) >= 3 and (ranked[0][0] - ranked[2][0]) < 0.04:
        return ""
    chosen = [(score, name) for score, name in ranked if score >= 0.35 and name.lower() not in style]
    if not chosen:
        chosen = [(score, name) for score, name in ranked if name.lower() not in style][:4]
    names_out = []
    used = set()
    drop_groups = [
        {"female vocal", "female voice", "female", "woman"},
        {"male vocal", "male voice", "male", "man"},
        {"vocal", "vocals", "voice", "singing"},
        {"no vocal", "no vocals", "no voice"},
        {"choir", "choral"},
    ]
    for score, name in chosen:
        key = name.lower()
        if key in used or key in style:
            continue
        group = next((g for g in drop_groups if key in g), None)
        if group and used & group:
            continue
        used.add(key)
        if group:
            used |= group
        names_out.append(name)
        if len(names_out) >= 6:
            break
    return ", ".join(names_out)


def predict(path, model="effnet"):
    if model not in ("effnet", "maest", "both", "mtt"):
        model = "effnet"
    needed = []
    if model in ("effnet", "both"):
        needed.append(MODEL)
        needed.append(LABELS)
    if model in ("maest", "both"):
        needed.append(MAEST)
        needed.append(MAEST_LABELS)
    if model in ("effnet", "maest", "both", "mtt"):
        needed.extend([MTT, MTT_LABELS])
    missing = [p.name for p in needed if not p.is_file()]
    if missing:
        raise FileNotFoundError("audio model missing: " + ", ".join(missing))
    bands = mels(load_audio(path))
    if model == "mtt":
        return {"subgenres": "", "einordnung": "", "jahr": None, "user1": mtt_tags(bands), "model": "mtt"}
    groups = []
    if model in ("effnet", "both"):
        groups.append(effnet_ranked(bands))
    if model in ("maest", "both"):
        groups.append(maest_ranked(bands))
    ranked = merge_ranked(*groups)
    kept = keep_styles(ranked)
    return {
        "subgenres": "/".join(kept),
        "einordnung": "",
        "jahr": None,
        "user1": mtt_tags(bands),
        "model": model,
        "scores": [{"name": name, "score": round(score, 3)} for score, name in ranked[:8]],
    }


if __name__ == "__main__":
    try:
        choice = sys.argv[2] if len(sys.argv) > 2 else "effnet"
        print(json.dumps(predict(sys.argv[1], choice), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
