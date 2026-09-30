# secretfill -- fill registered form fields from the macOS Keychain.
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

"""Fill sensitive personal fields into forms from the macOS Keychain.

Design rules:
  - Secret values never touch argv, shell history, or any file in this repo.
    `set` hands the prompt to /usr/bin/security itself (`-w` as the last option).
  - No command prints a secret unless the user explicitly asked for that value
    (`get`, `copy`). `fill` reports field names and counts only.
  - `fill` is a dry run unless --write is passed, and --write always leaves a
    timestamped backup of the original next to it.
  - The Keychain service name is the namespace: (service, account) is the
    keychain's unique key, so two projects can both hold an account called
    `phone_number` without colliding. Field names therefore stay bare.
  - Field FORMATS live in fields.toml, never here. What a valid phone number,
    national ID or date looks like is a property of the country issuing the
    form, not of this tool -- so a `format` hint and an optional `pattern`
    are declared per field, and a mismatch only warns. Ship this repo
    elsewhere and the registry is the only file that has to change.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tomllib
import unicodedata
import zipfile
from datetime import date, datetime
from pathlib import Path

SERVICE = "Happy-Job-Hunting"
HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "fields.toml"
REGISTRY_EXAMPLE = HERE / "fields.example.toml"

# __phone_number__ — the inner name may contain single underscores but can
# never span a "__", so adjacent placeholders cannot be swallowed into one match.
TOKEN_RE = re.compile(r"__([A-Za-z0-9]+(?:_[A-Za-z0-9]+)*)__")
WT_RE = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.DOTALL)

# Parts of a .docx that can carry visible text.
DOCX_TEXT_PARTS = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$"
)


# --------------------------------------------------------------------------- #
# derived date forms
# --------------------------------------------------------------------------- #
#
# Taiwanese forms ask for a birth date in either calendar, in several layouts.
# Only ONE value is stored (the ROC form, the user's choice) and every other
# form is rendered from it, so the two calendars can never drift apart.
#
# The bare name `birth_date` resolves from the other dates in the document
# (see BARE_DATE below). A wrong calendar on an official form is a real error, so whichever
# way it resolves is always printed rather than applied silently.

ROC_SOURCE = "birth_date_roc"

DERIVED_DATES = {
    "birth_date_roc":     ("出生年月日 · 民國（格式如 84/10/10）",
                           lambda y, m, d: f"{y}/{m:02d}/{d:02d}"),
    "birth_date_ce":      ("出生年月日 · 西元（格式如 1995/10/10）",
                           lambda y, m, d: f"{y + 1911}/{m:02d}/{d:02d}"),
    "birth_date_roc_cjk": ("出生年月日 · 民國中文（格式如 民國84年10月10日）",
                           lambda y, m, d: f"民國{y}年{m}月{d}日"),
    "birth_date_ce_cjk":  ("出生年月日 · 西元中文（格式如 1995年10月10日）",
                           lambda y, m, d: f"{y + 1911}年{m}月{d}日"),
}

# `__birth_date__` is accepted and resolves from the other dates in the document:
# Western-year dates only → CE, ROC-only evidence → ROC, both or none → CE (the
# default; real forms measured all used CE). Language share is not evidence. The
# choice is always printed — a silently wrong calendar on an official form is the
# failure mode here.
BARE_DATE = "birth_date"
# Storage stays ROC (the user's choice); the bare placeholder defaults to CE.
# Measured over the five real application forms on this machine, all three that
# carried any date evidence at all were filled out in CE and none in ROC — so
# CE is the better guess when the document gives nothing to go on.
DEFAULT_DATE_FORM = "birth_date_ce"
ROC_FORM = "birth_date_roc"

CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# What the document's OTHER date fields use is direct evidence; the language of
# the form is only a proxy. Measured over five real application forms, the
# proxy is not even monotonic — the most Chinese one (74% CJK) is filled out
# entirely in CE — because Taiwanese forms print an English subtitle under
# every field label, so the Latin count tracks template design, not calendar.
CE_EVIDENCE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*[/\-.年]\s*\d{1,2}")
# Only these are unambiguous ROC: an explicit 民國 marker, or a 3-digit year
# (100-199), which cannot be a 4-digit CE year or a 2-digit abbreviation.
ROC_EVIDENCE = re.compile(r"民國\s*\d{1,3}\s*年"
                          r"|(?<!\d)1\d{2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{1,2}(?!\d)")
# A 2-digit year is undecidable: 26/07/29 is 2026 here but 84/10/10 is 民國84.
SHORT_YEAR = re.compile(r"(?<!\d)\d{2}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{1,2}(?!\d)")


def pick_date_form(text: str) -> tuple[str, str, list[str]]:
    """Which calendar `__birth_date__` means here, why, and what to warn about.

    Precedence: unambiguous in-document date evidence first, then the CE
    default. The form's language is no longer a decision input — measured on
    real forms it does not predict the calendar (a 74%-Chinese form filled out
    entirely in CE) — but a Chinese form with no evidence is still where the
    default is most likely to be wrong, so that case is flagged.
    """
    ce = sorted(set(CE_EVIDENCE.findall(text)))
    roc = sorted(set(m for m in ROC_EVIDENCE.findall(text) if m))
    short = sorted(set(SHORT_YEAR.findall(text)))
    warn: list[str] = []

    if ce and not roc:
        return (DEFAULT_DATE_FORM,
                f"文件其他日期是西元（{', '.join(ce[:3])}…），共 {len(ce)} 處", warn)
    if roc and not ce:
        return (ROC_FORM,
                f"文件其他日期是民國（{', '.join(roc[:3])}…），共 {len(roc)} 處", warn)
    if ce and roc:
        warn.append(f"文件裡兩種曆法都有 —— 西元 {len(ce)} 處（{ce[0]}）、"
                    f"民國 {len(roc)} 處（{roc[0]}）。人工確認過再寫入")
        return DEFAULT_DATE_FORM, "兩種曆法並存，退回預設（西元）", warn

    # Nothing decisive in the document.
    if short:
        warn.append(f"文件有兩位數年份（{', '.join(short[:3])}），"
                    f"民國／西元無法分辨，沒有列入判斷")
    cjk = len(CJK_RE.findall(text))
    if cjk:
        warn.append(f"文件是中文表單（{cjk} 個中文字元）但沒有其他日期可依據 —— "
                    f"預設的西元最可能在這種情況判錯，要民國就改用 __{ROC_FORM}__")
    return DEFAULT_DATE_FORM, "文件無其他日期可依據，用預設（西元）", warn


def parse_roc(raw: str) -> tuple[int, int, int]:
    """Parse the stored ROC date into (roc_year, month, day).

    Tolerant on input layout so the value can be typed naturally, then every
    output form is rendered canonically — what was typed never leaks into the
    document's formatting.
    """
    s = raw.strip().replace("民國", "").replace("中華民國", "")
    runs = re.findall(r"\d+", s)
    if len(runs) == 3:
        y, m, d = (int(x) for x in runs)
    elif len(runs) == 1 and len(runs[0]) == 7:      # 0841010（身分證式）
        y, m, d = int(runs[0][:3]), int(runs[0][3:5]), int(runs[0][5:7])
    elif len(runs) == 1 and len(runs[0]) == 6:      # 841010
        y, m, d = int(runs[0][:2]), int(runs[0][2:4]), int(runs[0][4:6])
    else:
        die(f"{ROC_SOURCE} 的值看不出是日期：需要像 84/10/10、84年10月10日 或 0841010")

    if y > 200:
        hint = f"（{y} 年是民國 {y - 1911} 年）" if y > 1911 else ""
        die(f"{ROC_SOURCE} 存的是民國年，但讀到 {y}{hint}。請重新 set 成民國年。")
    if not 1 <= y <= 200:
        die(f"{ROC_SOURCE} 的民國年 {y} 不合理")
    try:
        date(y + 1911, m, d)
    except ValueError as e:
        die(f"{ROC_SOURCE} 不是有效日期（民國 {y} 年 {m} 月 {d} 日）：{e}")
    return y, m, d


def die(msg: str, code: int = 1):
    print(f"✗ {msg}", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

class Field:
    """One registered field: what it is, and optionally what shape it takes.

    `format` is a human hint shown when prompting and reporting; `pattern` is
    an optional regex that the stored value is checked against. Both are
    declared per field in fields.toml and NEVER hardcoded here — what counts
    as a valid phone number, ID or date is a property of the form-issuing
    country, not of this tool.
    """

    __slots__ = ("name", "label", "format", "pattern")

    def __init__(self, name: str, label: str,
                 fmt: str = "", pattern: str = ""):
        self.name = name
        self.label = label
        self.format = fmt
        self.pattern = pattern

    def describe(self) -> str:
        """Label plus format hint, for one-line reports."""
        return f"{self.label}｜格式 {self.format}" if self.format else self.label

    def check(self, value: str) -> str | None:
        """None if the value fits `pattern` (or there is none); else why not."""
        if not self.pattern:
            return None
        try:
            ok = re.fullmatch(self.pattern, value) is not None
        except re.error as e:
            return f"{self.name} 的 pattern 不是合法的正規表示式：{e}"
        if ok:
            return None
        shape = self.format or self.pattern
        return f"存的值不符合登記的格式（{shape}）"


def _field_from_entry(name: str, entry) -> Field:
    if isinstance(entry, str):
        return Field(name, entry)
    if isinstance(entry, dict):
        unknown = set(entry) - {"label", "format", "pattern"}
        if unknown:
            die(f"fields.toml 的 {name} 有不認識的鍵：{', '.join(sorted(unknown))}"
                f"（只接受 label、format、pattern）")
        if "label" not in entry:
            die(f"fields.toml 的 {name} 少了 label")
        f = Field(name, entry["label"], entry.get("format", ""),
                  entry.get("pattern", ""))
        if f.pattern:
            try:
                re.compile(f.pattern)
            except re.error as e:
                die(f"fields.toml 的 {name} pattern 編不起來：{e}")
        return f
    die(f"fields.toml 的 {name} 必須是字串或 table，讀到 {type(entry).__name__}")


def load_registry() -> tuple[dict[str, Field], str]:
    if not REGISTRY.exists():
        # 第一次使用時最可能走到這裡 —— 直接給下一步，不要只說檔案不見了。
        hint = (f"\n  先複製一份範本再改成你自己的欄位："
                f"\n      cp {REGISTRY_EXAMPLE.name} {REGISTRY.name}"
                if REGISTRY_EXAMPLE.exists() else "")
        die(f"找不到欄位登記表：{REGISTRY}{hint}")
    with REGISTRY.open("rb") as fh:
        data = tomllib.load(fh)
    fields = {name: _field_from_entry(name, entry)
              for name, entry in data.get("fields", {}).items()}
    return fields, data.get("prefix", "")


def resolve(name: str, fields: dict[str, Field], prefix: str) -> str | None:
    """Map a placeholder's inner name to a registered field.

    `prefix` in fields.toml is optional and normally empty — the keychain
    service already namespaces the accounts. When a prefix IS set, the bare
    form is accepted as an alias for the prefixed field.
    """
    if name in fields or name in DERIVED_DATES:
        return name
    if prefix:
        prefixed = f"{prefix}_{name}"
        if prefixed in fields:
            return prefixed
    return None


# --------------------------------------------------------------------------- #
# keychain
# --------------------------------------------------------------------------- #

def kc_get(field: str) -> str | None:
    """Read one value. Returns None if the item does not exist.

    `security -w` prints a lowercase hex dump instead of the value whenever the
    data contains a newline, a quote or a backslash — filling that hex into a
    form would be silent corruption, so we cross-check against `-g`, whose
    `password:` line carries an explicit `0x...` prefix in exactly those cases.
    """
    proc = subprocess.run(
        ["/usr/bin/security", "find-generic-password",
         "-s", SERVICE, "-a", field, "-w"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    plain = proc.stdout.rstrip("\n")

    g = subprocess.run(
        ["/usr/bin/security", "find-generic-password",
         "-s", SERVICE, "-a", field, "-g"],
        capture_output=True, text=True,
    )
    m = re.search(r"^password: 0x([0-9A-Fa-f]+)", g.stderr, re.MULTILINE)
    if m:
        try:
            plain = bytes.fromhex(m.group(1)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            die(f"{field} 在 Keychain 裡不是合法的 UTF-8 文字，拒絕填入")

    # C0 controls (and DEL) are not legal XML character data, so a stray one
    # would produce a .docx Word refuses to open — and a newline silently
    # breaks table layout. Both are set-time mistakes, caught at read time.
    bad = {c for c in plain if ord(c) < 0x20 or ord(c) == 0x7F}
    if bad:
        names = ", ".join(sorted(f"U+{ord(c):04X}" for c in bad))
        die(f"{field} 的值含控制字元（{names}），填進表單會壞掉；請重新 set 成單行純文字")
    return plain


def field_value(field: str) -> str | None:
    """Value of a stored account, or of a form derived from one."""
    if field in DERIVED_DATES:
        raw = kc_get(ROC_SOURCE)
        if raw is None:
            return None
        return DERIVED_DATES[field][1](*parse_roc(raw))
    return kc_get(field)


def kc_set(field: str) -> bool:
    """Prompt via /usr/bin/security so the value never reaches argv."""
    proc = subprocess.run(
        ["/usr/bin/security", "add-generic-password",
         "-U", "-s", SERVICE, "-a", field,
         "-D", f"{SERVICE} registered field",
         "-l", f"{SERVICE} {field}",
         "-w"],
    )
    return proc.returncode == 0


def kc_rm(field: str) -> bool:
    proc = subprocess.run(
        ["/usr/bin/security", "delete-generic-password",
         "-s", SERVICE, "-a", field],
        capture_output=True, text=True,
    )
    return proc.returncode == 0


# --------------------------------------------------------------------------- #
# token substitution
# --------------------------------------------------------------------------- #

def xml_escape(value: str) -> str:
    return (value.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;"))


def substitute_plain(text: str, values: dict[str, str]) -> tuple[str, dict[str, int]]:
    hits: dict[str, int] = {}

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in values:
            return m.group(0)
        hits[name] = hits.get(name, 0) + 1
        return values[name]

    return TOKEN_RE.sub(repl, text), hits


def substitute_docx_xml(xml: str, values: dict[str, str]) -> tuple[str, dict[str, int]]:
    """Replace placeholders inside a WordprocessingML part.

    Word freely splits a typed placeholder across several <w:t> runs (spell
    check, revision marks, autocorrect), so a plain regex over the XML misses
    them. We concatenate the text nodes of one paragraph, match there, then
    write the whole replacement into the first run it touched and blank the
    rest — which keeps the first run's formatting and survives any splitting.
    """
    hits: dict[str, int] = {}
    # Bound matching to a paragraph so a stray "__" can never pair across one.
    paragraphs = xml.split("</w:p>")
    for i, para in enumerate(paragraphs):
        nodes = list(WT_RE.finditer(para))
        if not nodes:
            continue
        joined = "".join(n.group(2) for n in nodes)
        if not TOKEN_RE.search(joined):
            continue

        spans: list[tuple[int, int, int]] = []  # (start, end, node index)
        pos = 0
        for idx, n in enumerate(nodes):
            length = len(n.group(2))
            spans.append((pos, pos + length, idx))
            pos += length

        new_text = [n.group(2) for n in nodes]
        replaced_any = False
        for m in reversed(list(TOKEN_RE.finditer(joined))):
            name = m.group(1)
            if name not in values:
                continue
            s, e = m.span()
            touched = [(a, b, idx) for (a, b, idx) in spans if a < e and b > s]
            if not touched:
                continue
            first_a, _, first_idx = touched[0]
            head = new_text[first_idx][: max(0, s - first_a)]
            last_a, _, last_idx = touched[-1]
            tail = new_text[last_idx][max(0, e - last_a):]
            for _, _, idx in touched:
                new_text[idx] = ""
            new_text[first_idx] = head + xml_escape(values[name])
            if last_idx == first_idx:
                new_text[first_idx] += tail
            else:
                new_text[last_idx] = tail
            hits[name] = hits.get(name, 0) + 1
            replaced_any = True

        if not replaced_any:
            continue

        out, cursor = [], 0
        for idx, n in enumerate(nodes):
            out.append(para[cursor:n.start()])
            open_tag = n.group(1)
            # xml:space="preserve" so a value with spaces is not collapsed
            if "xml:space" not in open_tag:
                open_tag = open_tag[:-1] + ' xml:space="preserve">'
            out.append(open_tag + new_text[idx] + n.group(3))
            cursor = n.end()
        out.append(para[cursor:])
        paragraphs[i] = "".join(out)

    return "</w:p>".join(paragraphs), hits


# --------------------------------------------------------------------------- #
# file drivers
# --------------------------------------------------------------------------- #

def scan_text(path: Path) -> str:
    """All visible text of the document, for language detection."""
    if path.suffix.lower() == ".docx":
        parts = []
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if DOCX_TEXT_PARTS.match(name):
                    xml = zf.read(name).decode("utf-8")
                    parts.append("".join(m.group(2) for m in WT_RE.finditer(xml)))
        return "\n".join(parts)
    return path.read_text(encoding="utf-8")


def scan_file(path: Path) -> list[str]:
    """Every placeholder inner-name occurrence, in document order."""
    if path.suffix.lower() == ".docx":
        found: list[str] = []
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if DOCX_TEXT_PARTS.match(name):
                    xml = zf.read(name).decode("utf-8")
                    for para in xml.split("</w:p>"):
                        joined = "".join(m.group(2) for m in WT_RE.finditer(para))
                        found += TOKEN_RE.findall(joined)
        return found
    return TOKEN_RE.findall(path.read_text(encoding="utf-8"))


def fill_file(path: Path, values: dict[str, str], out: Path) -> dict[str, int]:
    if path.suffix.lower() == ".docx":
        hits: dict[str, int] = {}
        with zipfile.ZipFile(path) as src:
            infos = src.infolist()
            payload = {i.filename: src.read(i.filename) for i in infos}
        for name in list(payload):
            if DOCX_TEXT_PARTS.match(name):
                new_xml, h = substitute_docx_xml(
                    payload[name].decode("utf-8"), values)
                payload[name] = new_xml.encode("utf-8")
                for k, v in h.items():
                    hits[k] = hits.get(k, 0) + v
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for i in infos:
                dst.writestr(i, payload[i.filename])
        return hits

    new_text, hits = substitute_plain(path.read_text(encoding="utf-8"), values)
    out.write_text(new_text, encoding="utf-8")
    return hits


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def _pad(text: str, width: int) -> str:
    """Pad to a DISPLAY width -- CJK characters occupy two terminal columns."""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - w)


def _disp_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def cmd_list(args):
    fields, prefix = load_registry()
    show = getattr(args, "values", False)
    width = max([len(k) for k in fields] + [len(k) for k in DERIVED_DATES] + [10])
    print(f"Keychain service: {SERVICE}")
    if prefix:
        print(f"欄位前綴: {prefix}_  ——  佔位符寫成 __{prefix}_<欄位>__")
    else:
        print("佔位符寫成 __<欄位>__")
    if show:
        print("⚠ --values：以下會把值印在畫面上。確認沒有人在看你的螢幕，"
              "也沒有在錄影或共享畫面。")
    print()

    vwidth = 0
    if show:
        vwidth = max([_disp_width(kc_get(n) or "") for n in fields] + [4])

    for name, field in fields.items():
        value = kc_get(name)
        mark = "✓ 已存" if value is not None else "· 未存"
        line = f"  {mark}  {name.ljust(width)}  "
        if show:
            line += _pad(value if value is not None else "—", vwidth) + "  "
        line += field.describe()
        if value is not None and (why := field.check(value)):
            line += f"\n         ⚠ {why}"
        print(line)

    derived = [n for n in DERIVED_DATES if n != ROC_SOURCE]
    if derived:
        has = kc_get(ROC_SOURCE) is not None
        print(f"\n  自動換算（從 {ROC_SOURCE} 算出來，不另外存）"
              f"{'' if has else ' —— 來源還沒存'}：")
        for name in derived:
            line = f"     {'↳' if has else '·'}  {name.ljust(width)}  "
            if show:
                value = field_value(name) if has else None
                line += _pad(value if value is not None else "—", vwidth) + "  "
            line += DERIVED_DATES[name][0]
            print(line)

    if show:
        print("\n（值只出現在這個畫面上 —— 沒有寫進任何檔案，也沒有進 shell history）")
    else:
        print("\n（只顯示有沒有存，不顯示值；全部的值用 list --values，"
              "單一欄位用 get，要貼用 copy）")


def _resolve_or_die(name: str) -> str:
    fields, prefix = load_registry()
    if name == BARE_DATE:
        return DEFAULT_DATE_FORM   # no document to read; default to domestic
    field = resolve(name, fields, prefix)
    if field is None:
        die(f"'{name}' 不在 fields.toml 裡。先去登記表加一行，避免打錯字存成孤兒欄位。")
    return field


def cmd_set(args):
    fields, prefix = load_registry()
    targets = []
    for name in args.fields:
        field = resolve(name, fields, prefix)
        if field is None:
            die(f"'{name}' 不在 fields.toml 裡。先去登記表加一行，避免打錯字存成孤兒欄位。")
        if field not in fields:
            die(f"'{field}' 是自動換算出來的，不能存。要改就 set {ROC_SOURCE}。")
        targets.append(field)
    for field in targets:
        spec = fields[field]
        print(f"\n[{field}] {spec.label}")
        if spec.format:
            print(f"  格式：{spec.format}")
        print("→ 輸入值（不會回顯，也不會進 shell history；security 會要求輸入兩次確認）")
        if not kc_set(field):
            die(f"{field} 寫入 Keychain 失敗")
        # 讀回來只為了對格式；值不會被印出來。
        stored = kc_get(field)
        if stored is not None and (why := spec.check(stored)):
            print(f"⚠ {field} 已存入，但{why}")
            print(f"  要改：uv run secretfill.py set {field}")
        else:
            print(f"✓ {field} 已存入 Keychain")


def cmd_get(args):
    field = _resolve_or_die(args.field)
    value = field_value(field)
    if value is None:
        die(f"{field} 還沒存進 Keychain")
    print(value)


def cmd_copy(args):
    field = _resolve_or_die(args.field)
    value = field_value(field)
    if value is None:
        die(f"{field} 還沒存進 Keychain")
    subprocess.run(["/usr/bin/pbcopy"], input=value, text=True, check=True)
    print(f"✓ {field} 已複製到剪貼簿（值沒有印出來）")


def cmd_rm(args):
    field = _resolve_or_die(args.field)
    if not kc_rm(field):
        die(f"{field} 刪除失敗（可能本來就沒存）")
    print(f"✓ {field} 已從 Keychain 刪除")


def cmd_fill(args):
    path = Path(args.file).expanduser().resolve()
    if not path.exists():
        die(f"找不到檔案：{path}")

    fields, prefix = load_registry()
    tokens = scan_file(path)
    if not tokens:
        print(f"檔案裡沒有任何 __欄位__ 佔位符：{path.name}")
        return

    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1

    doc_text = scan_text(path)
    date_form, date_why, date_warn = pick_date_form(doc_text)

    values: dict[str, str] = {}   # keyed by the name as written in the document
    ready, missing, unknown = [], [], []
    for name in counts:
        field = date_form if name == BARE_DATE else resolve(name, fields, prefix)
        if field is None:
            unknown.append(name)
            continue
        value = field_value(field)
        if value is None:
            missing.append((name, field))
        else:
            values[name] = value
            ready.append((name, field))

    print(f"檔案：{path}\n")
    if BARE_DATE in counts:
        print(f"  📅 __{BARE_DATE}__ 判定為 **{date_form}** —— {date_why}")
        for w in date_warn:
            print(f"     ⚠ {w}")
        if date_warn:
            print(f"     → 不確定就直接寫明確的 __birth_date_ce__ 或 __birth_date_roc__")
        print()

    def label_of(field: str) -> str:
        if field in DERIVED_DATES:
            return DERIVED_DATES[field][0]
        spec = fields.get(field)
        return spec.describe() if spec else ""

    def format_warning(field: str) -> str:
        """格式不符不擋寫入 —— 只是在寫進表單前先講一聲。"""
        spec = fields.get(field)
        if spec is None:
            return ""
        value = kc_get(field)
        if value is None:
            return ""
        why = spec.check(value)
        return f"\n           ⚠ {why}" if why else ""

    print("  已填 / 待補   佔位符 → Keychain 欄位")
    for name in sorted(counts):
        n = counts[name]
        field = date_form if name == BARE_DATE else resolve(name, fields, prefix)
        alias = "" if field == name or field is None else f" → {field}"
        if any(name == r[0] for r in ready):
            print(f"  ✓ 可填   __{name}__{alias} ×{n}  ({label_of(field)})"
                  f"{format_warning(field)}")
        elif any(name == m[0] for m in missing):
            src = ROC_SOURCE if field in DERIVED_DATES else field
            print(f"  ! 待補   __{name}__{alias} ×{n}  ({label_of(field)})"
                  f" —— Keychain 還沒 {src}")
        else:
            print(f"  ? 未登記 __{name}__ ×{n}  —— 不在 fields.toml，原樣保留")

    if missing:
        names = " ".join(sorted({ROC_SOURCE if f in DERIVED_DATES else f
                                 for _, f in missing}))
        print(f"\n  需要先存：uv run secretfill.py set {names}")
    if unknown:
        print(f"  未登記欄位（不會動它）：{', '.join('__' + u + '__' for u in sorted(unknown))}")

    # -o implies writing: sending the result somewhere else cannot destroy the
    # original, so requiring --write on top of it would be noise.
    out = Path(args.out).expanduser().resolve() if args.out else path
    if not (args.write or args.out):
        print("\n這是預演。寫回原檔加 --write，或用 -o 寫到別的路徑。")
        return
    if missing:
        die("有欄位還沒存進 Keychain，先補齊再寫入（避免寫出半成品表單）")

    # Back up whatever is about to be overwritten — the original for --write,
    # or an existing -o target.
    if out.exists() and not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = out.with_suffix(out.suffix + f".bak.{stamp}")
        shutil.copy2(out, backup)
        print(f"\n備份：{backup}")
    elif out.exists() and args.no_backup:
        print("\n⚠ --no-backup：直接覆寫，沒有備份")

    hits = fill_file(path, values, out)
    print(f"寫入：{out}")
    for name in sorted(hits):
        print(f"  ✓ 填入 __{name}__ ×{hits[name]}")

    mismatch = {k: v for k, v in counts.items()
                if k in values and hits.get(k, 0) != v}
    if mismatch:
        print("\n⚠ 下列佔位符的出現次數與實際填入次數不符，請打開檔案確認：")
        for k, v in mismatch.items():
            print(f"    __{k}__: 找到 {v} 次，填了 {hits.get(k, 0)} 次")
    left = sorted(unknown)
    if left:
        print(f"\n⚠ 仍留在檔案裡的佔位符：{', '.join('__' + u + '__' for u in left)}")
    print("\n⚠ 產出檔含個資：不要進版控、不要貼進對話。")


def main():
    p = argparse.ArgumentParser(
        prog="secretfill",
        description="把 macOS Keychain 裡的敏感個資填進表單（.docx / 純文字）",
        epilog="佔位符格式 __phone_number__（欄位名見 fields.toml）。",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="列出登記的欄位與是否已存（預設不顯示值）")
    ls.add_argument("--values", action="store_true",
                    help="連同值一起印出來（會顯示在畫面上，預設不開）")
    ls.set_defaults(fn=cmd_list)

    s = sub.add_parser("set", help="存入／更新一個或多個欄位（互動輸入）")
    s.add_argument("fields", nargs="+")
    s.set_defaults(fn=cmd_set)

    s = sub.add_parser("get", help="印出某欄位的值")
    s.add_argument("field")
    s.set_defaults(fn=cmd_get)

    s = sub.add_parser("copy", help="把某欄位複製到剪貼簿（不印出值）")
    s.add_argument("field")
    s.set_defaults(fn=cmd_copy)

    s = sub.add_parser("rm", help="從 Keychain 刪除某欄位")
    s.add_argument("field")
    s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("fill", help="檢查並取代佔位符（預設只做檢查，不寫入）")
    s.add_argument("file")
    s.add_argument("--write", action="store_true",
                   help="寫回原檔（預設會先留時間戳備份）")
    s.add_argument("-o", "--out", "--output", dest="out", metavar="FILE",
                   help="寫到別的路徑，原檔不動（不需要再加 --write）")
    s.add_argument("--no-backup", action="store_true",
                   help="不要留備份（預設會留 .bak.<時間戳>）")
    s.set_defaults(fn=cmd_fill)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
