#!/usr/bin/env python3
"""通話／面試錄音（macOS avfoundation → FLAC）。

錄出來的檔名與 `audio/` 既有的語料同格式（`YYYY-MM-DD HH.MM.SS.flac`），
所以錄完直接把路徑交給 transcribe.py（例如 `uv run transcribe.py audio/<檔名>.flac`），
不用改名也不用轉檔。transcribe.py 不會自己去 `audio/` 找檔。

用法：
    uv run record.py --list                 # 列出可用的輸入裝置
    uv run record.py                        # 用內建麥克風開錄，Enter 停
    uv run record.py -d "聚集裝置 - 內部/麥克風"   # 系統音＋麥克風（BlackHole 聚集）
    uv run record.py -d 2 --label acme-phone

🔴 頻寬的上限在上游，不在這支腳本。手機通話的對方語音在進到你的裝置之前
   就已經被電信網路的語音編碼壓成窄頻（AMR-NB 8k／VoLTE 的 AMR-WB 16k），
   48kHz 錄音救不回來已經丟掉的頻寬。這支腳本能贏的是三件事：
     ① 自己的聲音不經上行編碼（48kHz vs 手機錄音器的 12.4 kbps AMR-NB）
     ② 不做第二次有損重壓
     ③ 冗餘 —— 單次事件的錄音失敗沒有第二次機會
   用擴音讓手機喇叭進 Mac 麥克風，對方那半會比手機原生通話錄音更糟
   （多了房間殘響與喇叭失真）。要兩邊都好，通話本身要走 Mac。

🟢 雙軌分離的用法：用「聚集裝置」把系統音（對方）與麥克風（自己）各佔一個
   聲道錄下來，語者標記就不需要聲紋比對 —— 聲道本身就是答案。
   transcribe.py 目前是混成單聲道再判定，要用聲道分離得自己先 split。
   2026-09-21 實測本機「聚集裝置 - 內部/麥克風」＝3 聲道：
   ch0/ch1 是 BlackHole（系統音＝對方），ch2 是麥克風（自己）。分離指令：
     ffmpeg -i in.flac -filter_complex "pan=mono|c0=c0+c1" them.flac \
                       -filter_complex "pan=mono|c0=c2"    me.flac
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = HERE / "audio"
DEFAULT_DEVICE = "MacBook Pro Microphone"
# 既有語料全是 48kHz／16-bit FLAC，維持一致，比較才有意義。
DEFAULT_RATE = 48000
# 低於這個空間就先擋下來。48kHz 雙聲道 FLAC 約 5~10 MB/分鐘，
# 一小時的通話最多 600 MB，留兩倍餘裕。
MIN_FREE_GB = 2.0


def list_devices() -> list[tuple[int, str]]:
    """問 ffmpeg 要 avfoundation 的音訊輸入清單。

    ffmpeg 列完裝置一定以錯誤收場（沒有真的輸入檔），所以不看 returncode。
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
    )
    devices: list[tuple[int, str]] = []
    in_audio = False
    for line in proc.stderr.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        m = re.search(r"\[(\d+)\] (.+)$", line)
        if m:
            devices.append((int(m.group(1)), m.group(2).strip()))
    return devices


def resolve_device(spec: str, devices: list[tuple[int, str]]) -> int:
    """裝置指定可以是索引或名稱片段。

    預設走名稱比對而不是索引 —— 索引會隨著插拔耳機、開 Teams、iPhone 靠近
    而整排位移，寫死索引的腳本隔天就錄到別的東西。
    """
    if spec.isdigit():
        idx = int(spec)
        if any(i == idx for i, _ in devices):
            return idx
        raise SystemExit(f"⚠ 沒有索引 {idx} 這個音訊裝置。用 --list 看現在有什麼。")
    hits = [(i, name) for i, name in devices if spec.lower() in name.lower()]
    if not hits:
        raise SystemExit(f"⚠ 找不到名稱含「{spec}」的音訊裝置。用 --list 看現在有什麼。")
    if len(hits) > 1:
        names = "、".join(f"[{i}] {n}" for i, n in hits)
        raise SystemExit(f"⚠「{spec}」對到多個裝置：{names}。講得更精確一點，或直接給索引。")
    return hits[0][0]


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def mmss(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def record(out_path: Path, device_idx: int, rate: int, channels: int | None,
           limit: int | None) -> bool:
    """開錄，按 Enter 停。回傳是否錄到東西。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "avfoundation", "-i", f":{device_idx}",
        "-ar", str(rate),
    ]
    if channels:
        cmd += ["-ac", str(channels)]
    if limit:
        cmd += ["-t", str(limit)]
    # FLAC 無損，與既有語料同格式；-progress 走 stdout 才能自己畫進度。
    # 16-bit：既有語料全是 16-bit，而且 24-bit 對語音辨識沒有任何幫助，
    # 只是把檔案變兩倍大。
    cmd += ["-c:a", "flac", "-sample_fmt", "s16",
            "-progress", "pipe:1", "-nostats", str(out_path)]

    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    started = time.monotonic()
    print(f"\n🔴 錄音中 → {out_path.name}")
    print("   按 Enter 停止（Ctrl+C 也可以，檔案一樣會收尾）\n")

    # 讀 -progress 的輸出畫計時器。ffmpeg 每秒吐一組 key=value。
    import threading

    state = {"time": 0.0, "size": 0}

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            k, _, v = line.strip().partition("=")
            if k == "out_time_us" and v.isdigit():
                state["time"] = int(v) / 1_000_000
            elif k == "total_size" and v.isdigit():
                state["size"] = int(v)
            elif k == "progress":
                # 每個區塊的最後一個 key 才重畫，不然一秒會刷十幾次同一行。
                sys.stderr.write(
                    f"\r   {mmss(state['time'])}  {human_size(state['size']):>9}   "
                )
                sys.stderr.flush()

    threading.Thread(target=pump, daemon=True).start()

    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    sys.stderr.write("\r" + " " * 40 + "\r")

    # 'q' 讓 ffmpeg 正常收尾（FLAC 要寫完 header 的長度欄位才不會是壞檔）。
    try:
        proc.communicate("q", timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not out_path.exists() or out_path.stat().st_size < 1024:
        print("⚠ 沒有錄到聲音。第一次跑要在 macOS 的麥克風權限視窗按允許，然後重跑一次。")
        print("   （權限給的是跑這支腳本的終端機，不是 Python 本身）")
        out_path.unlink(missing_ok=True)
        return False

    dur = state["time"] or (time.monotonic() - started)
    print(f"✅ {out_path}")
    print(f"   長度 {mmss(dur)} · {human_size(out_path.stat().st_size)}")
    print(f"\n接著轉錄：\n   uv run transcribe.py {shlex_quote(str(out_path))}")
    return True


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def main() -> None:
    p = argparse.ArgumentParser(
        description="通話／面試錄音（macOS avfoundation → FLAC）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--list", action="store_true", help="列出音訊輸入裝置後結束")
    p.add_argument("-d", "--device", default=DEFAULT_DEVICE,
                   help=f"裝置索引或名稱片段（預設「{DEFAULT_DEVICE}」）")
    p.add_argument("-o", "--out-dir", default=str(DEFAULT_OUT_DIR),
                   help="輸出目錄（預設 audio/；transcribe.py 不會自己來找，轉錄時把路徑帶給它）")
    p.add_argument("--label", default="",
                   help="檔名後綴，例如 acme-phone（預設只有時間戳）")
    p.add_argument("--rate", type=int, default=DEFAULT_RATE, help=f"取樣率（預設 {DEFAULT_RATE}）")
    p.add_argument("--mono", action="store_true", help="強制單聲道（預設跟著裝置）")
    p.add_argument("--limit", type=int, default=None,
                   help="最長秒數的保險（預設不限；忘記按停也不會錄爆磁碟）")
    args = p.parse_args()

    if not shutil.which("ffmpeg"):
        raise SystemExit("⚠ 找不到 ffmpeg。brew install ffmpeg")

    devices = list_devices()
    if args.list:
        print("avfoundation 音訊輸入裝置：")
        for i, name in devices:
            mark = " ←預設" if name == DEFAULT_DEVICE else ""
            print(f"  [{i}] {name}{mark}")
        print("\n🟢 想同時錄下對方（系統音）與自己（麥克風），選「聚集裝置」那一項；")
        print("   聚集裝置要先在『音訊 MIDI 設定』裡把 BlackHole 與麥克風綁在一起。")
        return

    if not devices:
        raise SystemExit("⚠ 抓不到任何 avfoundation 音訊裝置。")

    device_idx = resolve_device(args.device, devices)
    device_name = dict(devices)[device_idx]

    out_dir = Path(args.out_dir).expanduser()
    if not out_dir.is_absolute():
        out_dir = HERE / out_dir
    free_gb = shutil.disk_usage(out_dir if out_dir.exists() else HERE).free / 1024 ** 3
    if free_gb < MIN_FREE_GB:
        raise SystemExit(f"⚠ 剩餘空間只有 {free_gb:.1f} GB，低於 {MIN_FREE_GB} GB。先清一下再錄。")

    # 與既有語料同一種檔名，transcribe.py 直接吃得下。
    stamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    name = f"{stamp} {args.label}".strip() if args.label else stamp
    out_path = out_dir / f"{name}.flac"

    print(f"裝置   [{device_idx}] {device_name}")
    print(f"格式   {args.rate} Hz · {'單聲道' if args.mono else '跟著裝置'} · FLAC")
    print(f"空間   剩 {free_gb:.0f} GB")
    if args.limit:
        print(f"上限   {mmss(args.limit)}")

    ok = record(out_path, device_idx, args.rate, 1 if args.mono else None, args.limit)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
