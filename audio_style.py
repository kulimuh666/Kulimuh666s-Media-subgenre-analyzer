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
MTT_WINDOW = (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(FRAME) / FRAME)).astype(np.float32)


def _hz_to_mel_slaney(hz):
    hz = np.asarray(hz, dtype=np.float64)
    slope = 3.0 / 200.0
    min_log_hz = 1000.0
    min_log_mel = min_log_hz * slope
    logstep = np.log(6.4) / 27.0
    linear = hz * slope
    above = hz >= min_log_hz
    return np.where(above, min_log_mel + np.log(np.maximum(hz, 1e-6) / min_log_hz) / logstep, linear)


def _mel_to_hz_slaney(mel):
    mel = np.asarray(mel, dtype=np.float64)
    slope = 3.0 / 200.0
    min_log_hz = 1000.0
    min_log_mel = min_log_hz * slope
    logstep = np.log(6.4) / 27.0
    linear = mel / slope
    above = mel >= min_log_mel
    return np.where(above, min_log_hz * np.exp(logstep * (mel - min_log_mel)), linear)


def _musicnn_filterbank():
    """Essentia TensorflowInputMusiCNN: Slaney mel, linear weight, unit_tri, power."""
    n_freq = FRAME // 2 + 1
    low = float(_hz_to_mel_slaney(0.0))
    high = float(_hz_to_mel_slaney(SR / 2.0))
    step = (high - low) / (BANDS + 1)
    freqs = _mel_to_hz_slaney(low + step * np.arange(BANDS + 2))
    scale = (SR / 2.0) / (n_freq - 1)
    fb = np.zeros((BANDS, n_freq), dtype=np.float64)
    for i in range(BANDS):
        left, center, right = float(freqs[i]), float(freqs[i + 1]), float(freqs[i + 2])
        fstep1 = center - left
        fstep2 = right - center
        jbegin = int(np.ceil(left / scale))
        jend = min(n_freq - 1, int(np.floor(right / scale)))
        for j in range(max(0, jbegin), jend + 1):
            binfreq = j * scale
            if binfreq < center:
                fb[i, j] = (binfreq - left) / fstep1 if fstep1 else 0.0
            else:
                fb[i, j] = (right - binfreq) / fstep2 if fstep2 else 0.0
        area = (fstep1 + fstep2) / 2.0
        if area:
            fb[i] /= area
    return fb.astype(np.float32)


MTT_FB = _musicnn_filterbank()
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


def musicnn_mels(audio):
    """Mel bands the MTT MusiCNN was trained on. Not the EffNet mel."""
    padded = np.concatenate([
        np.zeros(FRAME // 2, dtype=np.float32),
        np.asarray(audio, dtype=np.float32),
    ])
    n = 1 + max(0, (padded.size - FRAME) // HOP)
    need = (n - 1) * HOP + FRAME
    if padded.size < need:
        padded = np.concatenate([padded, np.zeros(need - padded.size, dtype=np.float32)])
    if n < 1:
        padded = np.zeros(FRAME, dtype=np.float32)
        n = 1
    frames = np.lib.stride_tricks.as_strided(
        padded,
        shape=(n, FRAME),
        strides=(padded.strides[0] * HOP, padded.strides[0]),
        writeable=False,
    ).copy()
    power = np.abs(np.fft.rfft(frames * MTT_WINDOW, axis=1)) ** 2
    return np.log10(1.0 + 10000.0 * (power @ MTT_FB.T)).astype(np.float32)


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
JAMENDO = ROOT / "models" / "mtg_jamendo_instrument-discogs-effnet-1.onnx"
JAMENDO_LABELS = ROOT / "models" / "mtg_jamendo_instrument-discogs-effnet-1.json"
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


def instrument_tags(bands):
    classes = json.loads(JAMENDO_LABELS.read_text(encoding="utf-8"))["classes"]
    skip = {"beat", "computer"}
    generic = {"drums", "synthesizer", "drummachine", "bass", "percussion", "sampler", "keyboard", "pad"}
    drop_if = {
        "guitar": {"electricguitar", "acousticguitar", "classicalguitar"},
        "drums": {"drummachine"},
        "piano": {"electricpiano"},
        "keyboard": {"electricpiano", "piano", "organ", "rhodes"},
    }
    pretty = {
        "acousticbassguitar": "Acoustic Bass",
        "acousticguitar": "Acoustic Guitar",
        "classicalguitar": "Classical Guitar",
        "electricguitar": "Electric Guitar",
        "electricpiano": "Electric Piano",
        "drummachine": "Drum Machine",
        "doublebass": "Double Bass",
        "pipeorgan": "Pipe Organ",
    }
    chunks = patches(bands)
    eff = session(MODEL)
    embeds = []
    for start in range(0, len(chunks), 16):
        raw = eff.run(["embeddings"], {eff.get_inputs()[0].name: chunks[start:start + 16].astype(np.float32)})[0]
        embeds.append(raw.reshape(raw.shape[0], -1)[:, :1280])
    emb = np.concatenate(embeds, axis=0)
    head = session(JAMENDO)
    parts = []
    for start in range(0, len(emb), 16):
        raw = head.run(["activations"], {head.get_inputs()[0].name: emb[start:start + 16]})[0]
        parts.append(raw.reshape(raw.shape[0], -1)[:, :len(classes)])
    mat = np.concatenate(parts, axis=0)
    keep = max(1, int(np.ceil(mat.shape[0] * 0.25)))
    robust = np.partition(mat, mat.shape[0] - keep, axis=0)[-keep:].mean(axis=0)
    ranked = sorted(
        ((float(robust[i]), classes[i]) for i in range(min(len(classes), robust.size)) if classes[i] not in skip),
        reverse=True,
    )
    present = {name for score, name in ranked if score >= 0.2}
    filtered = [(score, name) for score, name in ranked if not (drop_if.get(name) and present & drop_if[name])]
    distinctive = [(score, name) for score, name in filtered if name not in generic and score >= 0.25]
    chosen = distinctive[:6] or [(score, name) for score, name in filtered if name in generic and score >= 0.35][:3]
    labels = []
    for _, name in chosen:
        labels.append(pretty[name] if name in pretty else name[:1].upper() + name[1:])
    return ", ".join(labels)


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
        needed.extend([MODEL, JAMENDO, JAMENDO_LABELS])
    missing = [p.name for p in needed if not p.is_file()]
    if missing:
        raise FileNotFoundError("audio model missing: " + ", ".join(missing))
    audio = load_audio(path)
    bands = mels(audio)
    if model == "mtt":
        return {"subgenres": "", "einordnung": "", "jahr": None, "user1": instrument_tags(bands), "model": "jamendo"}
    groups = []
    if model in ("effnet", "both"):
        groups.append(effnet_ranked(bands))
    if model in ("maest", "both"):
        groups.append(maest_ranked(bands))
    ranked = merge_ranked(*groups)
    kept = keep_styles(ranked)
    return {
        "subgenres": " / ".join(kept),
        "einordnung": "",
        "jahr": None,
        "user1": instrument_tags(bands),
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
