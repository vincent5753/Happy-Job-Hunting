#!/usr/bin/env python3
"""錄音本地轉譯（面試、會議、通話；mlx-whisper / Apple Silicon）。

🔴 全程離線（預設 HF_HUB_OFFLINE=1）。錄音含公司內部架構與薪資，不得送雲端 API。
   只有第一次下載模型才需要連線：加 --allow-download 放行那一次，之後一律斷網。

用法：
    uv run transcribe.py audio/2026-01-15-phone.flac
    uv run transcribe.py audio/*.wav --groups core,aws,observability
    uv run transcribe.py rec.wav --groups core,hr --extra 範例公司,王先生 --formats txt,srt

支援格式：ffmpeg 讀得到的都行（wav / flac / m4a / mp3 / ogg / opus）。
不需要預先轉檔——Whisper 內部固定降成 16kHz 單聲道，手動先轉只會多一道失真。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import tomllib
from pathlib import Path

import keywords

DEFAULT_MODEL = "mlx-community/whisper-large-v3-mlx"
# Whisper 的重複偵測靠「溫度回退」：解出來的 compression_ratio 或 avg_logprob 不合格時，
# 用更高的溫度重跑那一段。傳單一 0.0 會**關掉整個機制**，靜音處就會無限重複或吐回 prompt。
# 這是在兩份真實面試錄音上實測到的（其中一份有 6.5 分鐘的重複段）。
DEFAULT_TEMPERATURE = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
# 溫度 > 0 的回退是隨機抽樣（mlx_whisper 只用 mx.random.categorical）。不固定種子的話，
# 同一檔跑兩次會在回退那一窗抽到不同文字，下一窗的起點跟著變，之後整段連鎖分岔
#（實測某份錄音在 384s 退到 1.0，兩次結果相似度只剩 0.92）。每個檔案開跑前重設，
# 結果才不受批次裡的檔案順序影響。
SAMPLING_SEED = 0
# Whisper 自己的重複判定門檻。壓縮比越高代表文字越重複。
# 實測：正常語句 0.27~0.85，幻覺重複段 8.17 與 13.26。
COMPRESSION_RATIO_LIMIT = 2.4
# 快速備援：趕時間或只要抓大意時換這個。
# 兩份面試錄音、兩種順序各跑一輪的實測：
#   turbo 快 1.57 倍（801s vs 1256s），但技術英文名詞會爛掉——
#   Docker→「Duck5」/「DAC」、VMware→「BMWELL」/「V&Wale」、ESXi→「EXXI」，
#   且簡體字洩漏是 large-v3 的四倍以上（同一份錄音：57~60 字 vs 11~15 字）。
# 72 分鐘的錄音只差 3 分鐘，所以預設用 large-v3，turbo 降為備援。
FALLBACK_MODEL = "mlx-community/whisper-large-v3-turbo"

# Whisper 的 initial_prompt 上限是 224 token（context 448 的一半），超過會被靜默截斷。
PROMPT_TOKEN_LIMIT = 224

# 提示詞回吐只在開頭這個時間窗內清（那是啟發式判定，窗外不敢動）。
LEAD_IN_WINDOW = 60.0
# (a) 提示詞回吐：large-v3 會把 initial_prompt 原封不動當成第一段講出來。
#     實測某份錄音兩輪的第一段都是「這段技術面試的對話：Linux、Proxmox VE、Kubernetes、Ceph、Ceph、Ceph。」
#     判定條件是同一段命中 3 個以上不同的提示詞——一般對話開場不會這樣。
PROMPT_ECHO_MIN_TERMS = 3
# (b) 訓練資料污染：Whisper 在中文靜音段會吐 YouTube 字幕組的招呼語。兩個模型都會。
#     這些是逐字固定的句子，技術面試裡不可能真的講出來，所以**不設時間窗**——
#     實測有一份這句出現在 00:02:24，60 秒的窗根本蓋不到（錄音開頭有 3 分鐘環境音）。
BOILERPLATE_PATTERNS = [
    # 只放「整句才成立」的高特異性片語。單獨的「訂閱」「轉發」「按讚」一律不放——
    # 曾用單詞版誤刪過兩段真實對話（「一個月五六千塊的AI的訂閱費」），
    # 這種詞在談 SaaS 成本的技術面試裡本來就會出現。
    r"不吝(点赞|點贊|按讚)",
    r"(点赞|點贊|按讚)[^。！？]{0,8}(订阅|訂閱)[^。！？]{0,8}(转发|轉發|打赏|打賞)",
    r"打赏支持|打賞支持",
    r"明镜与点点栏目|明鏡與點點欄目",
    r"字幕由.{0,12}(提供|制作|製作)",
    r"Amara\.org",
    r"(感谢|感謝)(观看|觀看|收看)[^。！？]{0,6}(下期再见|下期再見|订阅|訂閱)",
]


def estimate_tokens(text: str) -> int:
    """優先用 Whisper 自己的 tokenizer 精算；拿不到就用啟發式估。

    啟發式：CJK 約 1 字 1 token，其餘約 4 字元 1 token。寧可高估。
    """
    try:
        from mlx_whisper.tokenizer import get_tokenizer

        tok = get_tokenizer(multilingual=True, language="zh", task="transcribe")
        return len(tok.encoding.encode(text))
    except Exception:
        cjk = sum(1 for c in text if "一" <= c <= "鿿")
        return cjk + (len(text) - cjk) // 4 + 1


def fmt_ts(seconds: float, *, comma: bool = False) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    sep = "," if comma else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def filter_repetitive(segments: list[dict]) -> tuple[list[dict], int]:
    """丟掉重複幻覺段。兩個獨立訊號，任一成立就丟：

    1. `compression_ratio` 超過門檻——同一段內部自我重複（實測幻覺段 8~13）。
    2. 連續 3 段以上文字完全相同——跨段的鬼打牆迴圈，單段壓縮比看不出來。

    刻意不用 `no_speech_prob`：mlx-whisper 在這兩份實測錄音裡全部回 0.000，無鑑別力。
    """
    keep = [
        s for s in segments
        if s.get("compression_ratio", 0) <= COMPRESSION_RATIO_LIMIT
    ]
    out: list[dict] = []
    i = 0
    while i < len(keep):
        j = i
        text = keep[i]["text"].strip()
        while j + 1 < len(keep) and keep[j + 1]["text"].strip() == text:
            j += 1
        run = j - i + 1
        if run >= 3:
            # 連續 3 段以上一模一樣 = 鬼打牆迴圈，整串丟掉（保留原文沒有任何價值）
            pass
        else:
            # 1~2 段重複可能是真的（「對」「對」），原樣保留
            out.extend(keep[i : j + 1])
        i = j + 1
    return out, len(segments) - len(out)


def prompt_terms(prompt: str) -> list[str]:
    """把 initial_prompt 拆成可比對的詞。分隔符含中英文標點與頓號。"""
    parts = re.split(r"[、,，。：:；;／/（）()\s]+", prompt or "")
    return [t for t in parts if len(t) >= 2]


def filter_lead_in(segments: list[dict], prompt: str) -> tuple[list[dict], list[str]]:
    """清掉提示詞回吐（限開頭 LEAD_IN_WINDOW 秒）與字幕組招呼語（全檔）。

    兩者的把握程度不同，所以範圍也不同：
      - 提示詞回吐是啟發式判定（命中 N 個提示詞），可能誤傷，只在開頭清。
      - 字幕組招呼語是逐字固定的句子，面試裡不會真的講，全檔清。
    回傳 (保留的段, 被丟掉的原文) —— 丟掉的一定印出來，不做無聲刪除。
    """
    terms = prompt_terms(prompt)
    boiler = re.compile("|".join(BOILERPLATE_PATTERNS))
    out: list[dict] = []
    dropped: list[str] = []
    for s in segments:
        text = s["text"].strip()
        if s.get("start", 0.0) < LEAD_IN_WINDOW:
            hits = sum(1 for t in terms if t in text)
            if hits >= PROMPT_ECHO_MIN_TERMS:
                dropped.append(f"[提示詞回吐 {fmt_ts(s['start'])}] {text}")
                continue
        if boiler.search(text):
            dropped.append(f"[字幕組污染 {fmt_ts(s['start'])}] {text}")
            continue
        out.append(s)
    return out, dropped


# 轉繁體時保留原字的字。s2tw 對這幾個字的轉換在本機逐字稿上實測（2026-09-28）是誤判：
#   台→臺（違反台灣慣用）、只→隻（「是只有」變「是隻有」）、了→瞭（「了解」本來就對，不該動）。
S2TW_KEEP = frozenset("台了只")
_S2TW = None


def to_traditional(text: str) -> str:
    """OpenCC s2tw 轉繁體，S2TW_KEEP 的字保留原樣。

    為什麼需要：`condition_on_previous_text=False` 下每個 30 秒窗只靠 initial_prompt
    維持繁體，有些錄音會整段掉回簡體（實測兩通電話與一場視訊面試約 13%），調提示句壓不掉。
    只轉字形、不轉詞彙（不用 s2twp），不改寫對方實際講的話。
    """
    global _S2TW
    if _S2TW is None:
        import opencc
        _S2TW = opencc.OpenCC("s2tw")
    out = _S2TW.convert(text)
    if len(out) == len(text):
        return "".join(o if t in S2TW_KEEP else c for t, c, o in zip(text, out, text))
    # 詞組轉換改變長度時無法逐字對齊，改成把誤判的結果換回去
    for t, wrong in (("台", "臺"), ("只", "隻"), ("了", "瞭")):
        if wrong not in text:
            out = out.replace(wrong, t)
    return out


def traditionalize(segments: list[dict], result: dict) -> int:
    """把每段與全文轉繁體，回傳改動的段數。json 的 tokens 不動（那是模型原始輸出）。"""
    changed = 0
    for s in segments:
        new = to_traditional(s["text"])
        if new != s["text"]:
            s["text"] = new
            changed += 1
    if "text" in result:
        result["text"] = to_traditional(result["text"])
    return changed


def speaker_tag(seg: dict) -> str:
    """`[我] ` / `[對方] ` 前綴；低信度的加 `~`。沒跑 --diarize 就回空字串。"""
    who = seg.get("speaker")
    if not who:
        return ""
    mark = "~" if seg.get("speaker_low_confidence") else ""
    return f"[{who}]{mark} "


def write_txt(segments: list[dict], path: Path) -> None:
    """帶時間戳的純文字——面試複盤要能跳回某個時間點，純文字沒時間戳幾乎沒用。"""
    lines = [f"[{fmt_ts(s['start'])}] {speaker_tag(s)}{s['text'].strip()}"
             for s in segments]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_srt(segments: list[dict], path: Path) -> None:
    out = []
    for i, s in enumerate(segments, 1):
        out.append(
            f"{i}\n"
            f"{fmt_ts(s['start'], comma=True)} --> {fmt_ts(s['end'], comma=True)}\n"
            f"{speaker_tag(s)}{s['text'].strip()}\n"
        )
    path.write_text("\n".join(out), encoding="utf-8")


def write_json(result: dict, path: Path) -> None:
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def install_time_progress() -> None:
    """把 mlx-whisper 內建、以 mel frame 計數的進度條，改成以「音檔時間比例」呈現。

    mlx_whisper.transcribe 內部用 tqdm(total=content_frames)；frame 數與音檔秒數成
    線性正比，所以它的百分比本來就等於「已轉錄的音檔時間比例」。這裡只換顯示格式為
    百分比＋已用/預估剩餘時間、拿掉 frames 字樣，並只影響 mlx_whisper.transcribe。
    """
    import mlx_whisper  # 觸發 mlx_whisper.transcribe 子模組載入
    import tqdm as _tqdm

    # mlx_whisper.transcribe 這個名字被 __init__ re-export 的同名函式蓋掉，
    # 用 sys.modules 才拿得到「子模組」本身（＝該函式的 globals）。
    mt = sys.modules["mlx_whisper.transcribe"]

    class _TimeTqdm(_tqdm.tqdm):
        def __init__(self, *a, **k):
            if k.get("unit") == "frames":  # 只攔 transcribe 的那條進度條
                k["bar_format"] = (
                    "  轉錄 {percentage:3.0f}%|{bar}| 已用 {elapsed} · 預估剩 {remaining}"
                )
            super().__init__(*a, **k)

    class _Shim:
        tqdm = _TimeTqdm

    mt.tqdm = _Shim()


def transcribe_one(
    src: Path,
    *,
    model: str,
    language: str,
    prompt: str,
    out_dir: Path,
    formats: set[str],
    temperature: tuple[float, ...],
    drop_repetitive: bool,
    show_progress: bool,
    diarize: bool = False,
    speaker_target: str = "me",
    s2tw: bool = True,
) -> None:
    import mlx.core as mx
    import mlx_whisper

    print(f"\n▶ {src.name}")
    t0 = time.monotonic()
    mx.random.seed(SAMPLING_SEED)

    result = mlx_whisper.transcribe(
        str(src),
        path_or_hf_repo=model,
        language=language or None,
        initial_prompt=prompt or None,
        temperature=temperature,
        compression_ratio_threshold=COMPRESSION_RATIO_LIMIT,
        condition_on_previous_text=False,  # 長錄音下避免一次幻覺污染後面全部
        verbose=False if show_progress else None,  # False→顯示進度條；None→安靜
    )

    segments = result.get("segments", [])
    # 放在過濾之前：提示詞回吐的比對用的是繁體提示詞，先轉才比得到
    converted = 0
    if s2tw and str(result.get("language", "")).startswith("zh"):
        converted = traditionalize(segments, result)
    dropped = 0
    if drop_repetitive:
        segments, dropped = filter_repetitive(segments)
    segments, lead_in = filter_lead_in(segments, prompt)
    result["segments"] = segments
    elapsed = time.monotonic() - t0

    # 語者標記放在計時之後、寫檔之前。**任何失敗都只警告不中止** ——
    # 轉錄本身可能已經跑了好幾分鐘，不能因為聲紋沒註冊就把成果丟掉。
    diag = None
    diarize_elapsed = 0.0
    if diarize:
        import speaker
        try:
            # 全形字的顯示寬度是 2，清行要用足夠寬的空白才不會殘留字元，
            # 殘留會讓下一行的「音檔 …」統計被壓在同一行上。
            clear = "\r" + " " * 40 + "\r"
            print("  標記語者…", end="", flush=True)
            t_spk = time.monotonic()
            diag = speaker.annotate(segments, src, target=speaker_target)
            diarize_elapsed = time.monotonic() - t_spk
            print(clear, end="")
        except speaker.SpeakerError as e:
            print(f"{clear}  ⚠ 語者標記略過：{e}")
        except Exception as e:  # 非預期錯誤也不能吃掉轉錄結果
            print(f"{clear}  ⚠ 語者標記失敗（{type(e).__name__}: {e}）"
                  f"，逐字稿照常寫出")
    audio_len = segments[-1]["end"] if segments else 0.0

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    written = []
    if "txt" in formats:
        p = out_dir / f"{stem}.txt"
        write_txt(segments, p)
        written.append(p.name)
    if "srt" in formats:
        p = out_dir / f"{stem}.srt"
        write_srt(segments, p)
        written.append(p.name)
    if "json" in formats:
        p = out_dir / f"{stem}.json"
        write_json(result, p)
        written.append(p.name)

    speed = (audio_len / elapsed) if elapsed > 0 else 0
    note = f" · 濾掉 {dropped} 段重複/幻覺" if dropped else ""
    if converted:
        note += f" · {converted} 段簡體轉繁體"
    print(
        f"  音檔 {fmt_ts(audio_len)} · 轉錄 {elapsed:.1f}s "
        f"（約 {speed:.1f}x 即時）· {len(segments)} 段{note}"
    )
    for line in lead_in:
        print(f"  ✂ 開頭污染已移除 {line}")
    if diag:
        tt = diag["talk_time"]
        tot = sum(tt.values()) or 1.0
        print(f"  語者：{speaker.LABEL_ME} {tt[speaker.LABEL_ME]:.0f}s "
              f"({tt[speaker.LABEL_ME] / tot:.0%}) · "
              f"{speaker.LABEL_OTHER} {tt[speaker.LABEL_OTHER]:.0f}s "
              f"({tt[speaker.LABEL_OTHER] / tot:.0%})"
              f" · 耗時 {diarize_elapsed:.1f}s"
              f" · 場內門檻 {diag['threshold']:.3f}"
              f"（兩群間隔 {diag['gap']:.3f}，可信段 {diag['n_reliable']}）")
        if not diag["two_speakers"]:
            print(f"  🔴 兩群間隔低於 {speaker.MIN_CLUSTER_GAP}，"
                  f"這份錄音看起來只有一個人在講，語者標籤不可信")
    print(f"  → {out_dir}/{{{', '.join(written)}}}")


CONFIG_ENV = "TRANSCRIBE_CONFIG"
DEFAULT_CONFIG = Path(__file__).parent / "core.toml"


def load_config(path: Path | None) -> tuple[dict, Path | None]:
    """讀 core.toml 常用設定，回 (設定 dict, 實際讀到的路徑)；找不到回 ({}, None)。

    找檔順序：--config 指定 > $TRANSCRIBE_CONFIG > 腳本旁的 core.toml。
    設定可放在 [transcribe] 表底下，或直接放頂層。
    """
    if path is None:
        env = os.environ.get(CONFIG_ENV)
        path = Path(env).expanduser() if env else DEFAULT_CONFIG
    if not path.exists():
        return {}, None
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data.get("transcribe", data), path


def config_defaults(cfg: dict) -> dict:
    """把 core.toml 的值轉成 argparse 能吃的預設（型別對齊 add_argument）。

    groups / extra / formats / temperature 允許寫成陣列，這裡攤平成逗號字串；
    out_dir 相對路徑相對於腳本所在資料夾。
    """
    d: dict = {}
    if "model" in cfg:
        d["model"] = str(cfg["model"])
    if "language" in cfg:
        d["language"] = str(cfg["language"])
    for key in ("groups", "extra", "formats"):
        if key in cfg:
            v = cfg[key]
            d[key] = ",".join(map(str, v)) if isinstance(v, list) else str(v)
    if "temperature" in cfg:
        v = cfg["temperature"]
        d["temperature"] = (
            ",".join(str(x) for x in v) if isinstance(v, list) else str(v)
        )
    if "out_dir" in cfg:
        p = Path(str(cfg["out_dir"])).expanduser()
        d["out_dir"] = p if p.is_absolute() else Path(__file__).parent / p
    if "no_prompt" in cfg:
        d["no_prompt"] = bool(cfg["no_prompt"])
    if "keep_repetitive" in cfg:
        d["keep_repetitive"] = bool(cfg["keep_repetitive"])
    if "progress" in cfg:
        d["progress"] = bool(cfg["progress"])
    if "diarize" in cfg:
        d["diarize"] = bool(cfg["diarize"])
    if "speaker_target" in cfg:
        d["speaker_target"] = str(cfg["speaker_target"])
    if "s2tw" in cfg:
        d["s2tw"] = bool(cfg["s2tw"])
    return d


def main() -> int:
    # 先偷看 --config，才能在建立正式 parser 前把 core.toml 讀進來當預設。
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=Path, default=None,
                     help="設定檔路徑（預設讀腳本旁的 core.toml）")
    known, _ = pre.parse_known_args()
    cfg, cfg_path = load_config(known.config)

    ap = argparse.ArgumentParser(
        description="錄音本地轉譯（mlx-whisper）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"可用詞組：{', '.join(keywords.GROUPS)}",
        parents=[pre],
    )
    ap.add_argument("inputs", nargs="+", type=Path, help="音檔（wav/flac/m4a/mp3/...）")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"預設 {DEFAULT_MODEL}；只要抓大意、想快 1.6 倍改用 {FALLBACK_MODEL}")
    ap.add_argument("--language", default="zh", help="語言代碼，預設 zh；留空讓模型自動偵測")
    ap.add_argument("--groups", default=",".join(keywords.DEFAULT_GROUPS),
                    help="逗號分隔的詞組名稱")
    ap.add_argument("--extra", default="",
                    help="逗號分隔的額外詞（這場的公司名、對方姓氏、產品名）")
    ap.add_argument("--no-prompt", action="store_true", help="完全不用 initial_prompt")
    ap.add_argument("--out-dir", type=Path, default=Path(__file__).parent / "out")
    ap.add_argument("--formats", default="txt,srt", help="txt / srt / json，逗號分隔")
    ap.add_argument("--temperature",
                    default=",".join(str(t) for t in DEFAULT_TEMPERATURE),
                    help="逗號分隔的溫度回退序列。**不要只給一個 0**，那會關掉 Whisper "
                         "的重複偵測，靜音處會無限鬼打牆")
    ap.add_argument("--keep-repetitive", action="store_true",
                    help="保留重複/幻覺段（除錯用）")
    ap.add_argument("--no-progress", action="store_false", dest="progress",
                    default=True, help="不顯示轉錄進度條（依音檔時間比例）")
    ap.add_argument("--diarize", action="store_true",
                    help="標記每段是「我」還是「對方」（需先 speaker.py enroll 註冊聲紋）")
    ap.add_argument("--no-diarize", action="store_false", dest="diarize",
                    help="關掉語者標記（覆寫 core.toml 的 diarize = true）")
    ap.add_argument("--speaker-target", default="me", dest="speaker_target",
                    help="要標成「我」的那組聲紋名稱，預設 me")
    ap.add_argument("--no-s2tw", action="store_false", dest="s2tw", default=True,
                    help="中文輸出不轉繁體（預設用 OpenCC s2tw 轉，保留台／了／只）")
    ap.add_argument("--allow-download", action="store_true",
                    help="允許連線下載模型（只有第一次需要）；預設離線 HF_HUB_OFFLINE=1")
    # core.toml 蓋掉程式內建預設；命令列參數又蓋過 core.toml。
    ap.set_defaults(**config_defaults(cfg))
    args = ap.parse_args()
    if cfg_path:
        print(f"  ✓ 已套用設定檔 {cfg_path}")

    # 預設完全離線；只有明確 --allow-download 才放行連線下載模型。
    # huggingface_hub 在 import 時就固定讀這個環境變數，必須趕在載入 mlx_whisper 前設好
    #（本檔對 mlx_whisper 全是延遲 import，最早一次在下面 estimate_tokens）。
    if not args.allow_download:
        os.environ["HF_HUB_OFFLINE"] = "1"

    if not shutil.which("ffmpeg"):
        print("✗ 找不到 ffmpeg。mlx-whisper 靠它解碼音檔：brew install ffmpeg",
              file=sys.stderr)
        return 1

    files: list[Path] = []
    for p in args.inputs:
        if not p.exists():
            print(f"✗ 找不到檔案：{p}", file=sys.stderr)
            return 1
        files.append(p)

    # --- 組 prompt 並檢查 token 上限 ---
    prompt = ""
    if not args.no_prompt:
        groups = [g.strip() for g in args.groups.split(",") if g.strip()]
        extra = [w.strip() for w in args.extra.split(",") if w.strip()]
        try:
            prompt = keywords.build_prompt(groups, extra)
        except KeyError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1

        n = estimate_tokens(prompt)
        status = "✓" if n <= PROMPT_TOKEN_LIMIT else "🔴"
        print(f"{status} initial_prompt 約 {n} / {PROMPT_TOKEN_LIMIT} token"
              f"（詞組：{', '.join(groups)}{'；額外 ' + str(len(extra)) + ' 個' if extra else ''}）")
        if n > PROMPT_TOKEN_LIMIT:
            print("   超過上限，Whisper 會靜默截斷後半段——請減少詞組。", file=sys.stderr)
            return 1
        if n > keywords.SOFT_TOKEN_WARN:
            print(f"   ⚠ 超過 {keywords.SOFT_TOKEN_WARN} token。實測在這之上會開始出現標點污染"
                  "，且詞彙互相稀釋反而讓原本正確的詞變錯。建議只用 core + --extra。")

    try:
        temps = tuple(float(t) for t in str(args.temperature).split(",") if t.strip())
    except ValueError:
        print("✗ --temperature 必須是逗號分隔的數字", file=sys.stderr)
        return 1
    if len(temps) == 1:
        print("⚠ 只給一個溫度會關掉 Whisper 的重複偵測回退，靜音處容易鬼打牆。")

    formats = {f.strip() for f in args.formats.split(",") if f.strip()}
    if not formats <= {"txt", "srt", "json"}:
        print("✗ --formats 只接受 txt / srt / json", file=sys.stderr)
        return 1

    print(f"  模型 {args.model} · 語言 {args.language or 'auto'}")
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        print("  🔒 離線模式（HF_HUB_OFFLINE=1）；模型尚未快取時請加 --allow-download 先下載一次")
    else:
        print("  🌐 已放行連線下載模型（--allow-download）")

    if args.progress:
        install_time_progress()

    for src in files:
        try:
            transcribe_one(
                src,
                model=args.model,
                language=args.language,
                prompt=prompt,
                out_dir=args.out_dir,
                formats=formats,
                temperature=temps,
                drop_repetitive=not args.keep_repetitive,
                show_progress=args.progress,
                diarize=args.diarize,
                speaker_target=args.speaker_target,
                s2tw=args.s2tw,
            )
        except Exception as e:
            print(f"  ✗ {src.name} 失敗：{e}", file=sys.stderr)
            if os.environ.get("HF_HUB_OFFLINE") == "1":
                print("     （離線模式；若模型尚未下載，改加 --allow-download 先跑一次）",
                      file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
