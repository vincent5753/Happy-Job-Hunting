# secretfind -- scan folders for credentials stored in the macOS Keychain.
#
# Copyright (C) 2026  vincent5753 (Vincent Peng)
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-2.0-only

"""Scan folders for credentials stored in the macOS Keychain.

secretfill's inverse: secretfill puts Keychain values INTO a document,
secretfind checks that Keychain values are NOT in any file under a path.

Design rules:
  - Secret values never touch argv, shell history, or any file. `add` hands
    the prompt to /usr/bin/security itself (`-w` as the last option).
  - No command ever prints a value. A finding is reported as path, line,
    account name and which encoding matched -- never the matched bytes, and
    never the surrounding line (it may hold other secrets we do not know of).
  - The Keychain itself is the registry: every generic password under SERVICE
    is a credential to look for. There is no list file to drift out of sync.
  - A credential rarely leaks verbatim only. Each value is also searched as
    UTF-16LE, URL-encoded, JSON-escaped, XML-escaped and base64 (all three
    byte alignments), inside zip containers (.docx, .xlsx, .jar, ...), and
    in the extracted text of PDFs (pypdf), whitespace-insensitive.
  - What to scan and what to skip can live in a TOML config (--config);
    it holds paths and exclude patterns only, never a value.
  - Exit status is CI-friendly: 0 clean, 1 found something, 2 error.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import html
import io
import json
import logging
import os
import re
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, quote_plus

from pypdf import PdfReader

# pypdf 對格式不嚴謹的 PDF 會在 logging 噴一堆警告；解析失敗由 scan_pdf 自己回報。
logging.getLogger("pypdf").setLevel(logging.ERROR)

SERVICE = "secretfind"
SECURITY = "/usr/bin/security"

# 測試或另開 keychain 時用；平常不設，走預設 keychain。
KEYCHAIN_ENV = "SECRETFIND_KEYCHAIN"

ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")

# 短於這個長度的值很容易誤判（隨便一個單字都會命中），只掃原文、並且警告。
MIN_LEN = 8

# 預設排除：版控內部（物件是壓縮過的，逐位元組比對也找不到）與可重建的相依。
# 語法同 --exclude；--no-default-excludes 或 config 的 default_excludes = false 取消。
DEFAULT_EXCLUDES = [".git", ".hg", ".svn", ".venv", "venv", "node_modules",
                    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox"]

CONFIG_KEYS = {"paths", "exclude", "service", "max_size", "default_excludes"}

ZIP_MAGIC = b"PK\x03\x04"
PDF_MAGIC = b"%PDF-"
WS_RE = re.compile(r"\s+")
ZIP_MAX_DEPTH = 2          # 允許 zip 裡再包一層 zip（例如 .jar 裡的 .jar）
TAG_RE = re.compile(rb"<[^>]+>")


def die(msg: str, code: int = 2):
    print(f"✗ {msg}", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------- #
# keychain
# --------------------------------------------------------------------------- #

def keychain_args() -> list[str]:
    kc = os.environ.get(KEYCHAIN_ENV)
    return [kc] if kc else []


def _default_keychain() -> str:
    if kc := os.environ.get(KEYCHAIN_ENV):
        return kc
    proc = subprocess.run([SECURITY, "default-keychain"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"讀不到預設 keychain：{proc.stderr.strip()}")
    return proc.stdout.strip().strip('"')


def _attr(block: str, key: str) -> str | None:
    """One attribute out of a `security dump-keychain` item block.

    Plain ASCII prints as "acct"<blob>="name"; anything else (non-ASCII,
    quotes) prints as a bare hex dump, "acct"<blob>=0xE4B8AD... — both occur.
    """
    m = re.search(rf'^\s*"{key}"<blob>=(?:"(.*)"|0x([0-9A-Fa-f]+))', block,
                  re.MULTILINE)
    if not m:
        return None
    if m.group(1) is not None:
        return m.group(1)
    try:
        return bytes.fromhex(m.group(2)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def kc_accounts(service: str) -> list[str]:
    """Every generic-password account under `service`, without reading values.

    dump-keychain without -d prints attributes only, so this never touches
    secret data and never triggers an access prompt.
    """
    proc = subprocess.run([SECURITY, "dump-keychain", _default_keychain()],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"dump-keychain 失敗：{proc.stderr.strip()}")
    accounts = []
    for block in re.split(r"^keychain: ", proc.stdout, flags=re.MULTILINE):
        if 'class: "genp"' not in block or _attr(block, "svce") != service:
            continue
        if (acct := _attr(block, "acct")) is not None:
            accounts.append(acct)
    return sorted(set(accounts))


def kc_get(service: str, account: str) -> bytes | None:
    """Read one value as raw bytes. Returns None if the item does not exist.

    `security -w` prints a lowercase hex dump instead of the value whenever the
    data contains a newline, a quote or a backslash, so we cross-check against
    `-g`, whose `password:` line carries an explicit `0x...` prefix in exactly
    those cases (same trap as secretfill).
    """
    base = [SECURITY, "find-generic-password", "-s", service, "-a", account]
    proc = subprocess.run(base + ["-w"] + keychain_args(),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    value = proc.stdout.rstrip("\n").encode("utf-8")

    g = subprocess.run(base + ["-g"] + keychain_args(),
                       capture_output=True, text=True)
    if m := re.search(r"^password: 0x([0-9A-Fa-f]+)", g.stderr, re.MULTILINE):
        value = bytes.fromhex(m.group(1))
    return value


def kc_add(account: str) -> bool:
    """Prompt via /usr/bin/security so the value never reaches argv.

    `-w` only prompts when it is the very last argument, and the keychain
    path is a trailing positional too -- `-w <keychain>` would silently store
    the PATH as the password, `<keychain> -w` is a usage error. So there is
    no way to prompt into a non-default keychain; refuse instead of guessing.
    """
    proc = subprocess.run(
        [SECURITY, "add-generic-password",
         "-U", "-s", SERVICE, "-a", account,
         "-D", f"{SERVICE} credential",
         "-l", f"{SERVICE} {account}",
         "-w"],
    )
    return proc.returncode == 0


def kc_rm(account: str) -> bool:
    proc = subprocess.run(
        [SECURITY, "delete-generic-password", "-s", SERVICE, "-a", account]
        + keychain_args(),
        capture_output=True, text=True,
    )
    return proc.returncode == 0


# --------------------------------------------------------------------------- #
# needles
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Needle:
    account: str      # 顯示用：service/account
    encoding: str     # 命中的是哪一種寫法
    data: bytes


def _b64_cores(raw: bytes) -> list[tuple[str, bytes]]:
    """The part of base64(raw) that does not depend on the surrounding bytes.

    Inside a larger base64 blob (a k8s Secret, a Basic-auth header of
    "user:pass", a .p12 dump) the value may start at any of three byte
    alignments. For each, keep only the characters whose 6 bits come
    entirely from the value itself.
    """
    out = []
    for k in range(3):
        enc = base64.b64encode(b"\0" * k + raw)
        start = (8 * k + 5) // 6
        end = (8 * (k + len(raw))) // 6
        core = enc[start:end]
        if len(core) >= MIN_LEN:
            out.append((f"base64+{k}", core))
            url = core.replace(b"+", b"-").replace(b"/", b"_")
            if url != core:
                out.append((f"base64url+{k}", url))
    return out


def needles_for(label: str, raw: bytes) -> list[Needle]:
    variants: list[tuple[str, bytes]] = [("原文", raw)]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None

    if text is not None and len(raw) >= MIN_LEN:
        variants += [
            ("UTF-16LE", text.encode("utf-16-le")),
            ("URL", quote(text, safe="").encode()),
            ("URL+", quote_plus(text, safe="").encode()),
            ("JSON", json.dumps(text)[1:-1].encode()),
            ("XML", html.escape(text, quote=True).encode()),
        ]
    if len(raw) >= MIN_LEN:
        variants += _b64_cores(raw)

    seen: set[bytes] = set()
    out = []
    for enc, data in variants:
        if data and data not in seen:
            seen.add(data)
            out.append(Needle(label, enc, data))
    return out


def load_needles(services: list[str]) -> tuple[list[Needle], list[str]]:
    """All needles for every credential under `services`, plus warnings."""
    needles: list[Needle] = []
    warnings: list[str] = []
    for service in services:
        accounts = kc_accounts(service)
        if not accounts:
            warnings.append(f"service「{service}」底下沒有任何項目")
        for account in accounts:
            label = account if service == SERVICE else f"{service}/{account}"
            raw = kc_get(service, account)
            if raw is None:
                warnings.append(f"{label} 讀不到（被拒絕存取？）")
                continue
            if not raw:
                warnings.append(f"{label} 是空值，跳過")
                continue
            if len(raw) < MIN_LEN:
                warnings.append(f"{label} 只有 {len(raw)} bytes，容易誤判；"
                                f"只比對原文，不比對編碼變體")
            needles += needles_for(label, raw)
    return needles, warnings


# --------------------------------------------------------------------------- #
# scanning
# --------------------------------------------------------------------------- #

@dataclass
class Hit:
    where: str        # 檔案路徑，zip 內則是 a.docx!word/document.xml
    pos: str          # 行號；或「文字內容」（XML 去標籤後才命中）、「第 N 頁」（PDF）
    needle: Needle


def _find_all(data: bytes, needle: bytes):
    i = data.find(needle)
    while i != -1:
        yield i
        i = data.find(needle, i + 1)


def scan_bytes(where: str, data: bytes, needles: list[Needle],
               notes: list[str], depth: int = 0) -> list[Hit]:
    hits: list[Hit] = []
    for nd in needles:
        for pos in _find_all(data, nd.data):
            hits.append(Hit(where, str(data.count(b"\n", 0, pos) + 1), nd))

    # Word／Excel 會把一段打進去的文字拆成好幾個 run（<w:t>、<a:t>），逐位元組
    # 比對會漏掉。把標籤拿掉、還原實體後再找一次 —— 只補這個檔案裡還沒命中的 credential。
    if data.lstrip()[:1] == b"<":
        found = {h.needle.account for h in hits}
        text = html.unescape(TAG_RE.sub(b"", data).decode("utf-8", "replace"))
        flat = text.encode("utf-8")
        for nd in needles:
            if nd.account in found:
                continue
            if nd.data in flat:
                hits.append(Hit(where, "文字內容", nd))
                found.add(nd.account)

    if data[:5] == PDF_MAGIC:
        hits += scan_pdf(where, data, needles, notes,
                         found={h.needle.account for h in hits})

    if data[:4] == ZIP_MAGIC and depth < ZIP_MAX_DEPTH:
        hits += scan_zip(where, data, needles, notes, depth + 1)
    return hits


def _squash(s: str) -> str:
    return WS_RE.sub("", s)


def scan_pdf(where: str, data: bytes, needles: list[Needle],
             notes: list[str], found: set[str]) -> list[Hit]:
    """Search the extracted text of each page, whitespace removed on both sides.

    PDF text streams are usually Flate-compressed and often store glyph IDs
    rather than characters, so the raw-byte pass above sees nothing. pypdf
    maps glyphs back to text; whitespace is then dropped because layout
    breaks a long token at the line end (`ghp_abc` / `def`) and extraction
    may insert spaces between kerned fragments.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            notes.append(f"{where}：PDF 有密碼，沒有掃內文")
            return []
        pages = [_squash(page.extract_text() or "") for page in reader.pages]
    except Exception as e:      # pypdf 對壞檔丟的例外種類很雜，不能讓一個檔案中斷整次掃描
        notes.append(f"{where}：PDF 解析失敗，只比對了原始位元組（{type(e).__name__}）")
        return []

    if not any(pages):
        notes.append(f"{where}：PDF 抽不出文字（可能是掃描圖檔），沒有掃到內文")
        return []

    hits: list[Hit] = []
    for nd in needles:
        if nd.account in found:
            continue
        try:
            target = _squash(nd.data.decode("utf-8"))
        except UnicodeDecodeError:
            continue                # UTF-16LE 之類的位元組寫法，在抽出的文字裡沒有意義
        if not target:
            continue
        for i, text in enumerate(pages, 1):
            if target in text:
                hits.append(Hit(where, f"第 {i} 頁", nd))
                found.add(nd.account)
                break
    return hits


def scan_zip(where: str, data: bytes, needles: list[Needle],
             notes: list[str], depth: int) -> list[Hit]:
    hits: list[Hit] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                try:
                    member = zf.read(info)
                except (RuntimeError, zipfile.BadZipFile, NotImplementedError):
                    continue       # 加密或壞掉的成員：外層原文已經比對過了
                hits += scan_bytes(f"{where}!{info.filename}", member,
                                   needles, notes, depth)
    except zipfile.BadZipFile:
        pass
    return hits


@dataclass
class Stats:
    files: int = 0
    skipped_large: int = 0
    skipped_link: int = 0
    unreadable: int = 0
    excluded: int = 0


class Excluder:
    """gitignore-flavoured exclude patterns (fnmatch, not full gitignore).

      .git          no slash   → that name at any depth (file or directory)
      *.mp4         no slash   → glob on the name at any depth
      data/raw      has slash  → path relative to the scan root; a directory
      data/*/cache               match prunes everything under it
      ~/x, /abs/y   absolute   → that exact path and everything under it

    A matched directory is pruned, never descended into.
    """

    def __init__(self, patterns: list[str]):
        self.names: list[str] = []
        self.rels: list[str] = []
        self.absolutes: list[Path] = []
        for raw in patterns:
            pat = raw.strip().rstrip("/")
            if not pat:
                continue
            if pat.startswith(("/", "~")):
                self.absolutes.append(Path(pat).expanduser().resolve())
            elif "/" in pat:
                self.rels.append(pat.removeprefix("./"))
            else:
                self.names.append(pat)

    def __call__(self, path: Path, root: Path) -> bool:
        if any(fnmatch.fnmatchcase(path.name, p) for p in self.names):
            return True
        if self.rels:
            rel = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatchcase(rel, p) for p in self.rels):
                return True
        return any(path == a or a in path.parents for a in self.absolutes)


def walk(root: Path, excluded: Excluder, stats: Stats):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        kept = []
        for d in sorted(dirnames):
            if excluded(here / d, root):
                stats.excluded += 1
            else:
                kept.append(d)
        dirnames[:] = kept
        for name in sorted(filenames):
            p = here / name
            if excluded(p, root):
                stats.excluded += 1
                continue
            if p.is_symlink():
                stats.skipped_link += 1
                continue
            yield p


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_add(args):
    if keychain_args():
        die(f"設了 {KEYCHAIN_ENV} 時不能用 add：security 只有在 -w 是最後一個參數時才會跳 prompt，"
            f"而指定 keychain 也得放在最後。測試用的假值請直接 "
            f"security add-generic-password -s {SERVICE} -a <名稱> -w <假值> <keychain>")
    for account in args.accounts:
        if not ACCOUNT_RE.fullmatch(account):
            die(f"'{account}' 不是合法名稱（英數開頭，只能有英數 _ . -）")
    done: list[str] = []
    for account in args.accounts:
        print(f"\n[{account}]")
        print("→ 貼上或輸入值後按 Enter，接著再輸入一次確認（不一致會重問）。")
        print("  打字時畫面完全不會有反應（連 * 都沒有），這是正常的，不是卡住。"
              "值不會進 shell history。Ctrl-C 取消。")
        try:
            ok = kc_add(account)
        except KeyboardInterrupt:
            # Ctrl-C 也送給了同一個 process group 裡的 security，它不會寫到一半。
            saved = f"（{', '.join(done)} 已存入）" if done else ""
            die(f"已取消，{account} 沒有寫入{saved}", code=130)
        if not ok:
            die(f"{account} 寫入 Keychain 失敗")
        done.append(account)
        raw = kc_get(SERVICE, account)
        if raw is not None and len(raw) < MIN_LEN:
            print(f"⚠ {account} 已存入，但只有 {len(raw)} bytes —— 掃描時容易誤判")
        else:
            print(f"✓ {account} 已存入 Keychain")


def cmd_list(args):
    accounts = kc_accounts(SERVICE)
    print(f"Keychain service: {SERVICE}（{_default_keychain()}）\n")
    if not accounts:
        print("  （空的）用 add <名稱> 存入第一筆")
        return
    for account in accounts:
        raw = kc_get(SERVICE, account)
        if raw is None:
            note = "讀不到"
        elif len(raw) < MIN_LEN:
            note = f"⚠ {len(raw)} bytes，太短容易誤判"
        else:
            note = f"{len(raw)} bytes"
        print(f"  {account}  ({note})")
    print("\n（不顯示值；要看值請自己開「鑰匙圈存取」）")


def cmd_rm(args):
    if not kc_rm(args.account):
        die(f"{args.account} 刪除失敗（可能本來就沒存）")
    print(f"✓ {args.account} 已從 Keychain 刪除")


def load_config(path: Path) -> dict:
    """Read a scan config. Relative `paths` resolve against the config's directory.

    Every key is optional; unknown keys are rejected so a typo (`excludes`)
    cannot silently scan more than intended.
    """
    if not path.exists():
        die(f"找不到 config：{path}")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        die(f"{path.name} 不是合法的 TOML：{e}")
    unknown = set(data) - CONFIG_KEYS
    if unknown:
        die(f"{path.name} 有不認識的鍵：{', '.join(sorted(unknown))}"
            f"（只接受 {', '.join(sorted(CONFIG_KEYS))}）")
    for key in ("paths", "exclude", "service"):
        v = data.get(key, [])
        if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            die(f"{path.name} 的 {key} 必須是字串陣列")
    if "max_size" in data and not isinstance(data["max_size"], (int, float)):
        die(f"{path.name} 的 max_size 必須是數字（MB）")
    if "default_excludes" in data and not isinstance(data["default_excludes"], bool):
        die(f"{path.name} 的 default_excludes 必須是 true / false")

    base = path.parent
    data["paths"] = [str((base / Path(x).expanduser()).resolve())
                     for x in data.get("paths", [])]
    return data


def cmd_scan(args):
    # CLI 與 config 合併：路徑、排除規則、service 取聯集；max_size 以 CLI 為準。
    cfg = load_config(Path(args.config).expanduser().resolve()) if args.config else {}
    raw_paths = args.paths + cfg.get("paths", [])
    if not raw_paths:
        die("沒有要掃的路徑：在指令後面給路徑，或在 --config 裡寫 paths")
    roots = list(dict.fromkeys(Path(p).expanduser().resolve() for p in raw_paths))
    for r in roots:
        if not r.exists():
            die(f"找不到路徑：{r}")

    services = list(dict.fromkeys((args.service or []) + cfg.get("service", [])))
    services = services or [SERVICE]
    use_defaults = cfg.get("default_excludes", True) and not args.no_default_excludes
    patterns = ((DEFAULT_EXCLUDES if use_defaults else [])
                + cfg.get("exclude", []) + args.exclude)
    excluded = Excluder(patterns)
    max_size = args.max_size if args.max_size is not None else cfg.get("max_size", 50)

    needles, warnings = load_needles(services)
    for w in warnings:
        print(f"⚠ {w}", file=sys.stderr)
    if not needles:
        die("沒有任何 credential 可以比對。先用 add <名稱> 存進 Keychain")

    n_creds = len({nd.account for nd in needles})
    print(f"比對 {n_creds} 筆 credential（{len(needles)} 種寫法），"
          f"service：{', '.join(services)}", file=sys.stderr)

    max_bytes = int(max_size * 1024 * 1024)
    stats = Stats()
    hits: list[Hit] = []
    notes: list[str] = []
    for root in roots:
        base = root if root.is_dir() else root.parent
        for p in walk(root, excluded, stats):
            try:
                if p.stat().st_size > max_bytes:
                    stats.skipped_large += 1
                    continue
                data = p.read_bytes()
            except OSError:
                stats.unreadable += 1
                continue
            stats.files += 1
            rel = str(p.relative_to(base)) if len(roots) == 1 else str(p)
            hits += scan_bytes(rel, data, needles, notes)

    for h in hits:
        loc = f"{h.where}:{h.pos}" if h.pos.isdigit() else f"{h.where}:({h.pos})"
        print(f"{loc}\t{h.needle.account}\t{h.needle.encoding}")

    summary = [f"掃了 {stats.files} 個檔案"]
    if stats.excluded:
        summary.append(f"排除 {stats.excluded} 個檔案／目錄")
    if stats.skipped_large:
        summary.append(f"{stats.skipped_large} 個超過 {max_size:g} MB 跳過")
    if stats.skipped_link:
        summary.append(f"{stats.skipped_link} 個 symlink 跳過")
    if stats.unreadable:
        summary.append(f"{stats.unreadable} 個讀不到")
    print("\n" + "；".join(summary), file=sys.stderr)
    for n in notes:
        print(f"⚠ {n}", file=sys.stderr)
    print(f"排除規則：{', '.join(patterns) or '（無）'}", file=sys.stderr)

    if hits:
        files = len({h.where.split("!")[0] for h in hits})
        creds = len({h.needle.account for h in hits})
        print(f"✗ {creds} 筆 credential 出現在 {files} 個檔案，共 {len(hits)} 處",
              file=sys.stderr)
        raise SystemExit(1)
    print("✓ 沒有找到", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(
        prog="secretfind",
        description="掃描資料夾裡有沒有出現 macOS Keychain 裡登記的 credential",
        epilog="結束碼：0 沒找到、1 有找到、2 錯誤。值永遠不會印出來。",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="存入／更新一筆或多筆 credential（互動輸入）")
    s.add_argument("accounts", nargs="+", metavar="name")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("list", help="列出存了哪些 credential（只顯示名稱與長度）")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("rm", help="從 Keychain 刪除一筆 credential")
    s.add_argument("account", metavar="name")
    s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("scan", help="掃描路徑底下所有檔案")
    s.add_argument("paths", nargs="*", metavar="path",
                   help="要掃的檔案或目錄（可省略，改由 --config 的 paths 提供）")
    s.add_argument("-c", "--config", metavar="FILE",
                   help="TOML 設定檔：paths、exclude、service、max_size、"
                        "default_excludes（範本見 secretfind.example.toml）")
    s.add_argument("-e", "--exclude", action="append", default=[], metavar="PATTERN",
                   help="排除的檔案或路徑（可重複）。沒有 / 比對任何深度的名稱"
                        "（.git、*.mp4）；有 / 比對相對掃描根目錄的路徑（data/raw）；"
                        "/ 或 ~ 開頭是絕對路徑")
    s.add_argument("--no-default-excludes", action="store_true",
                   help=f"不套用預設排除（{', '.join(DEFAULT_EXCLUDES)}）")
    s.add_argument("--service", action="append", metavar="NAME",
                   help=f"改掃這個 service 底下的值（可重複；預設 {SERVICE}）。"
                        f"例如 --service Happy-Job-Hunting 掃 secretfill 的個資")
    s.add_argument("--max-size", type=float, default=None, metavar="MB",
                   help="超過這個大小的檔案跳過（預設 50）")
    s.set_defaults(fn=cmd_scan)

    args = p.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        print("\n✗ 已取消", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
