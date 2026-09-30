#!/usr/bin/env bash
# timer.sh —— 終端機計時器（通知 + 音效 + 語音）
#
# 為什麼不用 Clock.app 或 Shortcuts：
#   · Clock.app 不可腳本化 —— Info.plist 沒有 NSAppleScriptEnabled、沒有
#     OSAScriptingDefinition，是 Catalyst app，沒有 AppleScript 字典。
#     `open -a Clock` 只能把它打開，設不了計時器。
#   · /usr/bin/shortcuts 只能 run / list / view / sign 既有捷徑，不能新建。
#     要新建得自己組 WFWorkflow plist 再簽章，而且還是得手動匯入一次。
#   所以這裡完全不碰那兩個，直接用 osascript + afplay + say。
#
# 用法：
#   ./timer.sh 5                    # 5 分鐘（純數字＝分鐘）
#   ./timer.sh 90s                  # 90 秒
#   ./timer.sh 1h30m "去吃飯"        # 1 小時 30 分，自訂訊息
#   ./timer.sh 3:30                 # mm:ss
#   ./timer.sh 1:02:30              # hh:mm:ss
#   ./timer.sh 25 -b                # 背景跑，終端機立刻還你
#   ./timer.sh 5 -n 3               # 到時音效響 3 次
#   ./timer.sh 5 -q                 # 不要語音
#   ./timer.sh --list-sounds

set -euo pipefail

MSG=""
SOUND="Glass"
VOICE=""            # 空＝依訊息語言自動選
QUIET=0
BACKGROUND=0
REPEAT=1
DUR=""

SOUND_DIR="/System/Library/Sounds"

die() { printf '%s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
timer.sh —— 終端機計時器（通知 + 音效 + 語音）

用法：
  ./timer.sh 5                    # 5 分鐘（純數字＝分鐘）
  ./timer.sh 90s                  # 90 秒
  ./timer.sh 2m30s                # 混合單位
  ./timer.sh 3:30                 # mm:ss
  ./timer.sh 1:02:30              # hh:mm:ss
  ./timer.sh 1h30m "去吃飯"        # 自訂到時訊息

選項：
  -b, --background      背景跑，終端機立刻還你（關掉終端機也還在）
  -s, --sound NAME      到時音效（預設 Glass；--list-sounds 看清單）
  -n, --repeat N        音效重複幾次（預設 1）
  -v, --voice NAME      指定語音（預設：訊息含中文用 Meijia，否則系統預設）
  -q, --quiet           不要語音，只有通知與音效
      --list-sounds     列出可用音效
  -h, --help            這份說明
USAGE
    exit 0
}

list_sounds() {
    printf '可用音效（-s 帶名字，不含 .aiff）：\n'
    /bin/ls "$SOUND_DIR" | sed 's/\.aiff$//' | sed 's/^/  /'
    exit 0
}

# 把 "5" / "90s" / "1h30m" / "3:30" / "1:02:30" 解析成秒數
parse_duration() {
    local s="$1" total=0
    if [[ "$s" =~ ^[0-9]+$ ]]; then
        # 🔴 純數字一律當「分鐘」。這是刻意的：計時器最常用的單位是分鐘，
        #    要秒就寫 90s。預設成秒會讓 `timer.sh 5` 這種最短指令變得沒用。
        printf '%s' $(( s * 60 )); return
    fi
    if [[ "$s" == *:* ]]; then
        local IFS=':' parts=() p
        read -r -a parts <<< "$s"
        for p in "${parts[@]}"; do
            [[ "$p" =~ ^[0-9]+$ ]] || die "時間格式看不懂：$s"
            total=$(( total * 60 + p ))
        done
        printf '%s' "$total"; return
    fi
    local rest="$s" num unit
    while [[ -n "$rest" ]]; do
        [[ "$rest" =~ ^([0-9]+)([hms])(.*)$ ]] || die "時間格式看不懂：$s"
        num="${BASH_REMATCH[1]}"; unit="${BASH_REMATCH[2]}"; rest="${BASH_REMATCH[3]}"
        case "$unit" in
            h) total=$(( total + num * 3600 )) ;;
            m) total=$(( total + num * 60 ))   ;;
            s) total=$(( total + num ))        ;;
        esac
    done
    printf '%s' "$total"
}

fmt_hms() {
    local t="$1"
    printf '%02d:%02d:%02d' $(( t / 3600 )) $(( t % 3600 / 60 )) $(( t % 60 ))
}

while (( $# )); do
    case "$1" in
        -h|--help)        usage ;;
        --list-sounds)    list_sounds ;;
        -s|--sound)       SOUND="${2:?-s 要帶音效名}"; shift 2 ;;
        -v|--voice)       VOICE="${2:?-v 要帶語音名}"; shift 2 ;;
        -n|--repeat)      REPEAT="${2:?-n 要帶次數}"; shift 2 ;;
        -q|--quiet)       QUIET=1; shift ;;
        -b|--background)  BACKGROUND=1; shift ;;
        -*)               die "不認識的參數：$1（--help 看用法）" ;;
        *)                if [[ -z "$DUR" ]]; then DUR="$1"; else MSG="$1"; fi; shift ;;
    esac
done

[[ -n "$DUR" ]] || usage
SECS="$(parse_duration "$DUR")"
(( SECS > 0 )) || die "時間要大於 0"
[[ -f "$SOUND_DIR/$SOUND.aiff" ]] || die "找不到音效 ${SOUND}（--list-sounds 看清單）"
[[ "$REPEAT" =~ ^[0-9]+$ ]] && (( REPEAT >= 1 )) || die "-n 要是正整數"

END_HM="$(date -v +"${SECS}"S +%H:%M:%S)"
[[ -n "$MSG" ]] || MSG="$(fmt_hms "$SECS") 到了"

# 語音：訊息含中日韓字就用中文語音，否則交給系統預設（Meijia 念英文很慘）
if [[ -z "$VOICE" ]]; then
    if LC_ALL=en_US.UTF-8 grep -qP '[\x{4e00}-\x{9fff}]' <<< "$MSG" 2>/dev/null \
       || python3 -c 'import sys;sys.exit(0 if any("一"<=c<="鿿" for c in sys.argv[1]) else 1)' "$MSG" 2>/dev/null; then
        VOICE="Meijia"
    fi
fi

# 🔴 背景模式先於倒數：重新 exec 自己（去掉 -b）並 disown，
#    這樣關掉終端機也還在跑，而且不必把觸發邏輯寫兩份。
if (( BACKGROUND )); then
    args=( "$DUR" "$MSG" -s "$SOUND" -n "$REPEAT" )
    (( QUIET )) && args+=( -q )
    [[ -n "$VOICE" ]] && args+=( -v "$VOICE" )
    nohup "$0" "${args[@]}" >/dev/null 2>&1 &
    printf '⏳ 背景計時 %s（PID %s）→ %s　%s\n' "$(fmt_hms "$SECS")" "$!" "$END_HM" "$MSG"
    exit 0
fi

fire() {
    /usr/bin/osascript -e "display notification \"${MSG//\"/\\\"}\" with title \"⏰ 時間到\" sound name \"$SOUND\"" 2>/dev/null || true
    local i
    for (( i = 0; i < REPEAT; i++ )); do
        /usr/bin/afplay "$SOUND_DIR/$SOUND.aiff" 2>/dev/null || true
    done
    if (( ! QUIET )); then
        if [[ -n "$VOICE" ]]; then
            /usr/bin/say -v "$VOICE" "$MSG" 2>/dev/null || true
        else
            /usr/bin/say "$MSG" 2>/dev/null || true
        fi
    fi
}

cleanup() { printf '\r\033[K⏹  取消（剩 %s）\n' "$(fmt_hms "$remain")"; exit 130; }
trap cleanup INT TERM

printf '⏳ %s → %s　%s\n' "$(fmt_hms "$SECS")" "$END_HM" "$MSG"
remain="$SECS"
while (( remain > 0 )); do
    # 只在有 tty 時畫倒數，被重導向到檔案時不要塞一堆 \r
    [[ -t 1 ]] && printf '\r\033[K   剩 %s ' "$(fmt_hms "$remain")"
    sleep 1
    remain=$(( remain - 1 ))
done
[[ -t 1 ]] && printf '\r\033[K'
printf '⏰ 時間到 %s　%s\n' "$(date +%H:%M:%S)" "$MSG"
fire
