#!/usr/bin/env python3
"""語者標記：用註冊聲紋把每個 segment 標成「我」或「對方」。

這支模組是語者標記原型（spike，未公開）的產品化版本。方法與參數都是在
六場真實面試錄音上驗證過的（23/23 命中）：

  · 不訓練、不 fine-tune。聲紋是一個 192 維向量（centroid），由本人幾段
    乾淨語音的 embedding 平均而來。多一份錄音就重算平均，成本為零。
  · 判定門檻**不是固定值**，而是每份錄音自己導出來的。跨場實測各場最佳門檻
    從 0.256（8kHz 電話）到 0.486（實體面試）相差 0.23，固定門檻會在通道一換
    就誤判；場內把分數分兩群則不受通道漂移影響，因為兩群會一起平移。
  · 不跑獨立 VAD。切在 Whisper 的段界上，標籤與文字天生對齊；而且
    sherpa-onnx 1.10.46 不支援上游現在釋出的 silero_vad 模型（Init 直接 abort）。

已知限制：
  · 一個 Whisper segment 中間若換了人，整段只會拿到一個標籤。
  · 短於 MIN_RELIABLE_SEC 的段落照樣判定，但標為低信度（`~`）——
    embedding 在 1.5 秒以下本來就不穩，使用者要看得出哪幾行不能當證據。
  · 只分「我 / 對方」。多位面試官不會被彼此分開。

CLI（轉錄流程用 transcribe.py --diarize，這裡只做註冊與模型下載）：
  uv run speaker.py fetch-models
  uv run speaker.py enroll <labels.tsv> <audio>
  uv run speaker.py show
"""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SPEAKER_DIR = HERE / "speaker"
MODELS = SPEAKER_DIR / "models"
ENROLL = SPEAKER_DIR / "enroll"

# 中英雙語版：面試講中文但技術名詞全是英文（Kubernetes、Ansible、Proxmox），
# 純 zh-cn 的模型在這種夾雜語料上沒有驗證過比較好。
EMBED_MODEL = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
MODEL_BASE = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
              "speaker-recongition-models")  # 上游 tag 真的拼錯，不是筆誤

SAMPLE_RATE = 16000

# embedding 要算得準需要足夠長的單人語音。短於此仍judge，但標為低信度。
MIN_RELIABLE_SEC = 1.5

# 兩群平均至少要差這麼多，才認定錄音裡真的有兩個人。低於此值多半是單人錄音，
# 硬分兩群只會把雜訊切開——這時候寧可不標，也不要給一份看起來正常的錯誤輸出。
MIN_CLUSTER_GAP = 0.15

LABEL_ME = "我"
LABEL_OTHER = "對方"


class SpeakerError(RuntimeError):
    """語者標記失敗。轉錄本身已經完成，呼叫端應該警告而不是中止。"""


# --------------------------------------------------------------------------- #

def model_path() -> Path:
    p = MODELS / EMBED_MODEL
    if not p.exists():
        raise SpeakerError(
            f"找不到聲紋模型 {p.relative_to(HERE)}，先跑 `uv run speaker.py fetch-models`")
    return p


def _extractor(num_threads: int = 4):
    try:
        import sherpa_onnx
    except ImportError as e:  # pragma: no cover
        raise SpeakerError(f"sherpa-onnx 匯入失敗：{e}") from e
    cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(model_path()), num_threads=num_threads, provider="cpu")
    if not cfg.validate():
        raise SpeakerError(f"聲紋模型設定驗證失敗：{model_path()}")
    return sherpa_onnx.SpeakerEmbeddingExtractor(cfg)


def _embed(ex, samples: np.ndarray) -> np.ndarray | None:
    """算一段語音的 embedding；太短就回 None。"""
    if len(samples) < int(0.25 * SAMPLE_RATE):
        return None
    stream = ex.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples.astype(np.float32).tolist())
    stream.input_finished()
    if not ex.is_ready(stream):
        return None
    vec = np.asarray(ex.compute(stream), dtype=np.float32)
    return vec / (np.linalg.norm(vec) + 1e-12)


def load_audio_16k(src: Path) -> np.ndarray:
    """借 mlx-whisper 的 ffmpeg 讀檔：已經是 16kHz 單聲道 float32。

    自己做 48k→16k 抽樣會 alias（整數 3 倍但沒先低通），一律交給 ffmpeg。
    """
    from mlx_whisper.audio import load_audio
    # load_audio 回的是 mlx.core.array，不是 numpy —— 必須轉，否則後面
    # .astype(np.float32) 會打到 mlx 的 astype 而型別不符。
    return np.asarray(load_audio(str(src), sr=SAMPLE_RATE), dtype=np.float32)


def load_centroid(name: str = "me") -> np.ndarray:
    p = ENROLL / f"{name}.npy"
    if not p.exists():
        have = sorted(q.stem for q in ENROLL.glob("*.npy")) if ENROLL.is_dir() else []
        raise SpeakerError(
            f"還沒有 {name} 的聲紋（{p.relative_to(HERE)} 不存在）"
            f"{'；現有：' + '、'.join(have) if have else ''}。"
            f"先跑 `uv run speaker.py enroll <labels.tsv> <audio>`")
    return np.load(p)


def fit_relative(scores: list[float], durations: list[float]) -> dict:
    """場內把分數分成兩群，回傳判定門檻與診斷。

    1D 的 2-means，初始化在 min/max。一維兩群時 Lloyd 收斂到全域最佳，所以
    不需要隨機重啟——同一份錄音每次跑結果都一樣（驗證需要這個性質）。
    """
    vals = np.array([s for s, d in zip(scores, durations) if d >= MIN_RELIABLE_SEC],
                    dtype=np.float64)
    if len(vals) < 10:
        raise SpeakerError(f"可信段落只有 {len(vals)} 個，不足以分群")
    lo, hi = float(vals.min()), float(vals.max())
    for _ in range(100):
        mid = (lo + hi) / 2
        left, right = vals[vals < mid], vals[vals >= mid]
        if not len(left) or not len(right):
            break
        nlo, nhi = float(left.mean()), float(right.mean())
        if abs(nlo - lo) < 1e-9 and abs(nhi - hi) < 1e-9:
            break
        lo, hi = nlo, nhi
    return {"threshold": (lo + hi) / 2, "mean_other": lo, "mean_me": hi,
            "gap": hi - lo, "n_reliable": int(len(vals)),
            "two_speakers": (hi - lo) >= MIN_CLUSTER_GAP}


def annotate(segments: list[dict], src: Path, *, target: str = "me",
             on_progress=None) -> dict:
    """就地給每個 segment 加上 `speaker` 與 `speaker_score`。

    回傳診斷 dict。失敗一律丟 SpeakerError——轉錄已經完成，呼叫端要保住成果。
    """
    if not segments:
        raise SpeakerError("沒有 segment 可標記")
    tvec = load_centroid(target)
    ex = _extractor()
    samples = load_audio_16k(src)

    scores, durs = [], []
    for i, seg in enumerate(segments):
        a = int(seg["start"] * SAMPLE_RATE)
        b = int(seg["end"] * SAMPLE_RATE)
        vec = _embed(ex, samples[max(a, 0):min(b, len(samples))])
        # 算不出來的極短段給 0：落在對方側，並且靠 duration 標成低信度。
        scores.append(float(np.dot(vec, tvec)) if vec is not None else 0.0)
        durs.append(seg["end"] - seg["start"])
        if on_progress and (i + 1) % 200 == 0:
            on_progress(i + 1, len(segments))

    fit = fit_relative(scores, durs)
    thr = fit["threshold"]
    for seg, sc, d in zip(segments, scores, durs):
        seg["speaker"] = LABEL_ME if sc >= thr else LABEL_OTHER
        seg["speaker_score"] = round(sc, 4)
        seg["speaker_low_confidence"] = d < MIN_RELIABLE_SEC
    fit["talk_time"] = {
        LABEL_ME: round(sum(d for s, d in zip(scores, durs) if s >= thr), 1),
        LABEL_OTHER: round(sum(d for s, d in zip(scores, durs) if s < thr), 1),
    }
    return fit


# --------------------------------------------------------------------------- #
# CLI：註冊與模型下載
# --------------------------------------------------------------------------- #

def read_labels(path: Path) -> list[dict]:
    """手寫的語者標記檔。**絕不從逐字稿自動產生**——標記是 ground truth，
    自動產生出錯會靜默污染 centroid，而且錯得看不出來。"""
    rows = []
    for lineno, raw in enumerate(path.read_text("utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cells = [c.strip() for c in line.split("\t")]
        if cells[0] == "speaker":
            continue
        if len(cells) < 4:
            sys.exit(f"🔴 {path.name}:{lineno} 欄位不足（speaker/start/end/use）")
        rows.append({"speaker": cells[0], "start": float(cells[1]),
                     "end": float(cells[2]), "use": cells[3],
                     "note": cells[4] if len(cells) > 4 else "", "lineno": lineno})
    if not rows:
        sys.exit(f"🔴 {path.name} 沒有任何標記列")
    return rows


def cmd_fetch_models(_args) -> int:
    MODELS.mkdir(parents=True, exist_ok=True)
    dest = MODELS / EMBED_MODEL
    if dest.exists():
        print(f"已存在 {dest.relative_to(HERE)}")
        return 0
    print(f"下載 {EMBED_MODEL}")
    subprocess.run(["curl", "-fsSL", "-o", str(dest),
                    f"{MODEL_BASE}/{EMBED_MODEL}"], check=True)
    print(f"→ {dest.relative_to(HERE)}")
    return 0


def cmd_enroll(args) -> int:
    rows = read_labels(Path(args.labels))
    samples = load_audio_16k(Path(args.audio))
    ex = _extractor()
    ENROLL.mkdir(parents=True, exist_ok=True)

    by_speaker: dict[str, list[np.ndarray]] = {}
    for row in rows:
        if row["use"] != "enroll":
            continue
        dur = row["end"] - row["start"]
        if dur < MIN_RELIABLE_SEC:
            sys.exit(f"🔴 第 {row['lineno']} 列只有 {dur:.1f}s，低於 "
                     f"{MIN_RELIABLE_SEC}s 不可信，請改長一點的段落")
        a, b = int(row["start"] * SAMPLE_RATE), int(row["end"] * SAMPLE_RATE)
        vec = _embed(ex, samples[a:b])
        if vec is None:
            sys.exit(f"🔴 第 {row['lineno']} 列算不出 embedding")
        by_speaker.setdefault(row["speaker"], []).append(vec)
        print(f"  ✓ {row['speaker']:6s} {row['start']:8.1f}-{row['end']:8.1f}s "
              f"({dur:5.1f}s)  {row['note']}")
    if not by_speaker:
        sys.exit("🔴 標記檔裡沒有 use=enroll 的列")

    for speaker, vecs in by_speaker.items():
        out = ENROLL / f"{speaker}.npy"
        # 已有聲紋就把舊的一起平均進來：跨場、跨房間、跨通道的素材越多，
        # centroid 越不受單一錄音環境影響。
        if args.add and out.exists():
            vecs = vecs + [np.load(out)]
            print(f"  ◦ --add：併入既有的 {out.name}")
        centroid = np.mean(vecs, axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-12
        np.save(out, centroid)
        pairs = [float(np.dot(a, b)) for a, b in itertools.combinations(vecs, 2)]
        spread = (f"段間一致性 {min(pairs):.3f}~{max(pairs):.3f}" if pairs
                  else "單一段落")
        print(f"→ {out.relative_to(HERE)}  由 {len(vecs)} 段平均 · {spread}")
        (ENROLL / f"{speaker}.meta.json").write_text(json.dumps(
            {"speaker": speaker, "n_segments": len(vecs), "model": EMBED_MODEL,
             "labels": Path(args.labels).name, "audio": Path(args.audio).name,
             "dim": int(centroid.shape[0])}, ensure_ascii=False, indent=2), "utf-8")
    return 0


def cmd_show(_args) -> int:
    print(f"模型目錄 {MODELS.relative_to(HERE)}"
          f"  {'✓ ' + EMBED_MODEL if (MODELS / EMBED_MODEL).exists() else '✗ 未下載'}")
    if not ENROLL.is_dir() or not any(ENROLL.glob("*.npy")):
        print("聲紋：（無）")
        return 0
    for p in sorted(ENROLL.glob("*.npy")):
        meta = ENROLL / f"{p.stem}.meta.json"
        info = json.loads(meta.read_text("utf-8")) if meta.exists() else {}
        print(f"聲紋 {p.stem:8s} {info.get('dim', '?')} 維 · "
              f"由 {info.get('n_segments', '?')} 段平均 · "
              f"來源 {info.get('audio', '?')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="語者聲紋註冊（標記走 transcribe.py --diarize）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch-models", help="下載聲紋模型")
    p = sub.add_parser("enroll", help="依手寫標記檔建立／更新聲紋")
    p.add_argument("labels")
    p.add_argument("audio")
    p.add_argument("--add", action="store_true",
                   help="與既有聲紋合併平均（多場素材可提高跨通道穩定度）")
    sub.add_parser("show", help="列出已下載的模型與已註冊的聲紋")
    args = ap.parse_args()
    return {"fetch-models": cmd_fetch_models, "enroll": cmd_enroll,
            "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
