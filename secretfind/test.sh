#!/usr/bin/env bash
# secretfind 的端對端測試。
#
#     bash test.sh          # 在任何目錄跑都可以
#     bash test.sh -v       # 連每一項的完整輸出一起印
#
# 用拋棄式 keychain 存假值（SECRETFIND_KEYCHAIN），login keychain 完全不碰；
# 測試資料放 mktemp 目錄。結束時（含失敗與 Ctrl-C）一律刪掉 keychain 與測試資料。
#
# 需要 macOS 的 security、textutil、iconv。有 soffice（LibreOffice）才測 PDF，沒有就跳過那一項。
#
# 互動輸入（add 的 prompt、Ctrl-C）沒辦法在這裡自動測，見檔尾。

set -euo pipefail
cd "$(dirname "$0")"

VERBOSE=0
[[ "${1:-}" == "-v" ]] && VERBOSE=1

W=$(mktemp -d)
K="$W/test.keychain-db"
cleanup() {
    security delete-keychain "$K" 2>/dev/null || true
    rm -rf "$W"
}
trap cleanup EXIT

# --------------------------------------------------------------------------- #
# 拋棄式 keychain 與假值
# --------------------------------------------------------------------------- #

security create-keychain -p x "$K"
security unlock-keychain -p x "$K"
SECRET='sk-test/AB+cd&ef9xyz'
LONG=$(printf 'ghp_LongToken%.0s' {1..12})XYZ      # 159 字元，PDF 一行放不下會斷行
security add-generic-password -s secretfind -a api_key    -w "$SECRET" "$K"
security add-generic-password -s secretfind -a long_token -w "$LONG"   "$K"
export SECRETFIND_KEYCHAIN="$K"

# --------------------------------------------------------------------------- #
# 測試資料：repo/ 放各種寫法，other/ 給「指令列路徑取代 config」用
# --------------------------------------------------------------------------- #

T="$W/repo"
mkdir -p "$T"/{sub,.git,node_modules/x,data/raw,logs} "$W/other/logs"
printf 'a\nKEY=%s\n' "$SECRET"                          > "$T/sub/.env"     # 原文，第 2 行
printf '{"k": "%s"}' "$SECRET"                          > "$T/cfg.json"
printf 'postgres://u:sk-test%%2FAB%%2Bcd%%26ef9xyz@h/db\n' > "$T/url.txt"   # URL 編碼
printf 'data:\n  key: %s\n' "$(printf 'user:%s' "$SECRET" | base64)" > "$T/k8s.yaml"   # base64
printf '%s' "$SECRET" | iconv -f UTF-8 -t UTF-16LE      > "$T/w.ps1"        # UTF-16LE
printf '<p>sk-test/AB+cd&amp;ef9xyz</p>'                > "$T/page.html"    # XML 實體
printf '%s' "$SECRET" | tee "$T/.git/leak" "$T/node_modules/x/leak" "$T/data/raw/leak" \
                            "$T/logs/a.log" "$W/other/o.txt" "$W/other/logs/o.log" >/dev/null
echo 'nothing here'                                     > "$T/clean.txt"

printf '<html><body><p>key: %s</p></body></html>' "$SECRET" > "$W/src.html"
textutil -convert docx "$W/src.html" -output "$T/form.docx"                # docx

PDF=0
if command -v soffice >/dev/null; then
    printf '<html><body><p>x</p><p style="page-break-before:always">%s</p></body></html>' \
        "$LONG" > "$W/long.html"
    soffice --headless --convert-to pdf --outdir "$T" "$W/long.html" >/dev/null 2>&1
    [[ -f "$T/long.pdf" ]] && PDF=1                                         # token 在第 2 頁且被斷行
fi

cat > "$W/scan.toml" <<'TOML'
paths   = ["repo"]
exclude = ["*.log", "data/*", "sub"]
TOML
echo 'excludes = ["x"]' > "$W/bad.toml"

# --------------------------------------------------------------------------- #
# 斷言
# --------------------------------------------------------------------------- #

PASS=0
FAIL=0

# check <說明> <預期結束碼> <預期命中數｜-> [stdout 或 stderr 必須包含的字串...] -- <secretfind 參數...>
# 命中數是 stdout 的行數（每一行是一筆命中）；- 表示不檢查。
check() {
    local desc=$1 want_rc=$2 want_hits=$3
    shift 3
    local needles=()
    while [[ $1 != "--" ]]; do needles+=("$1"); shift; done
    shift

    local out err rc=0
    out=$(uv run -q secretfind.py "$@" 2>"$W/stderr") || rc=$?
    err=$(<"$W/stderr")

    local problems=()
    [[ $rc == "$want_rc" ]] || problems+=("結束碼 $rc，預期 $want_rc")
    if [[ $want_hits != "-" ]]; then
        local hits
        hits=$(grep -c . <<<"$out" || true)
        [[ $hits == "$want_hits" ]] || problems+=("命中 $hits 處，預期 $want_hits")
    fi
    local n
    for n in "${needles[@]+"${needles[@]}"}"; do
        grep -qF -- "$n" <<<"$out$err" || problems+=("輸出裡沒有「$n」")
    done

    if (( ${#problems[@]} == 0 )); then
        PASS=$((PASS + 1))
        echo "✓ $desc"
    else
        FAIL=$((FAIL + 1))
        echo "✗ $desc"
        printf '    %s\n' "${problems[@]}"
    fi
    if (( VERBOSE || ${#problems[@]} )); then
        sed 's/^/    │ /' <<<"$out"
        sed 's/^/    ┆ /' <<<"$err"
    fi
}

# 共用的命中位置（pdf 視 soffice 而定）
P=$PDF

check "list 只顯示名稱與長度"            0 -  "api_key  (20 bytes)" "long_token  (159 bytes)" -- list

check "各種寫法都抓到；.git、node_modules 預設排除" \
      1 $((9 + P)) \
      "sub/.env:2	api_key	原文" "url.txt:1	api_key	URL" "k8s.yaml:2	api_key	base64+2" \
      "w.ps1:1	api_key	UTF-16LE" "page.html:1	api_key	XML" \
      "form.docx!word/document.xml:2	api_key	XML" \
      -- scan "$T"
if (( PDF )); then
    check "PDF：斷行的長 token 在第 2 頁"   1 -  "long.pdf:(第 2 頁)	long_token	原文" -- scan "$T/long.pdf"
else
    echo "- 跳過 PDF（沒有 soffice）"
fi

check "-e 排除檔名與相對路徑"            1 $((7 + P))  -- scan "$T" -e '*.log' -e data/raw
check "--no-default-excludes 連 .git 一起掃" \
      1 $((11 + P)) ".git/leak:1" "node_modules/x/leak:1" -- scan "$T" --no-default-excludes

check "config：paths 相對設定檔目錄"      1 $((6 + P))  -- scan -c "$W/scan.toml"
check "指令列路徑取代 config 的 paths"     1 1  "略過 scan.toml 的 paths" "o.txt:1" -- scan -c "$W/scan.toml" "$W/other"
check "-e 和 config 的 exclude 合併"       0 0  "沒有找到" -- scan -c "$W/scan.toml" "$W/other" -e o.txt

check "乾淨的檔案"                         0 0  "沒有找到" -- scan "$T/clean.txt"
check "config 打錯鍵直接報錯"              2 -  "不認識的鍵：excludes" -- scan -c "$W/bad.toml"
check "沒給路徑也沒有 config"              2 -  "沒有要掃的路徑" -- scan
check "路徑不存在"                         2 -  "找不到路徑" -- scan /nonexistent
check "設了 SECRETFIND_KEYCHAIN 時拒絕 add" 2 - "不能用 add" -- add demo

echo
echo "通過 $PASS、失敗 $FAIL"
(( FAIL == 0 ))

# --------------------------------------------------------------------------- #
# 互動輸入要手動測（用的是 login keychain，測完記得 rm）：
#
#   uv run secretfind.py add selftest   # 第一個 prompt 按 Ctrl-C → 「已取消，selftest 沒有寫入」，沒有 traceback
#   uv run secretfind.py add selftest   # 輸入同一個值兩次 → 「✓ selftest 已存入」；不一致會被重問
#   uv run secretfind.py rm selftest
# --------------------------------------------------------------------------- #
