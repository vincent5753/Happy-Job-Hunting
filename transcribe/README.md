# transcribe — 錄音本地轉譯（面試、會議、通話）

以 [mlx-whisper](https://github.com/ml-explore/mlx-examples)（Apple Silicon）做的**全程離線**語音轉文字（腳本預設 `HF_HUB_OFFLINE=1`）。
只有第一次下載模型才需要連線，用 `--allow-download` 明確放行那一次，之後完全斷網——錄音常含公司內部資訊與薪資，不送雲端 API。

給 agent 的操作規則在 [`CLAUDE.md`](CLAUDE.md)。

## 檔案

| 檔案 | 功能 |
|------|------|
| `transcribe.py` | 轉譯主程式：反幻覺過濾、中文轉繁體（s2tw）、固定亂數種子、進度條、輸出 `txt`／`srt`／`json`，可加語者標記（`--diarize`） |
| `keywords.py` | `initial_prompt` 詞組。開頭有實測紀錄：詞組選太多反而會讓原本對的詞變錯 |
| `speaker.py` | 語者標記：用本人的聲紋把每段標成「我」或「對方」；負責註冊聲紋與下載模型 |
| `record.py` | 通話／面試／會議錄音（macOS avfoundation → FLAC），檔名格式直接接得上 `transcribe.py` |
| `core.toml` | 常用設定（模型、語言、詞組、輸出格式、是否做語者標記） |
| `speaker/labels.example.tsv` | 語者標記檔的格式範例（tab 分隔：speaker／start／end／use／note） |

## 常用指令

```bash
cd transcribe
uv sync                                        # 選用：先把環境建好（uv run 第一次也會自動建）
uv run transcribe.py <音檔> --allow-download   # 首次下載模型（唯一需要連線的時候）
uv run transcribe.py <音檔>                    # 之後全離線，走 core.toml 預設
uv run transcribe.py <音檔> --extra 公司名,面試官姓氏
uv run transcribe.py <音檔> --model <本機模型路徑>
uv run transcribe.py <音檔> --language en      # 英文錄音；預設 zh 會把英文翻成中文

uv run record.py --list                        # 列出錄音裝置
uv run record.py -d 2 --label <場次>           # 開錄，Enter 停

uv run speaker.py fetch-models                 # 語者標記模型（首次）
uv run speaker.py enroll <labels.tsv> <音檔>        # 用手寫標記檔註冊本人聲紋（格式見 speaker/labels.example.tsv）；🔴 會覆蓋舊聲紋
uv run speaker.py enroll <labels.tsv> <音檔> --add  # 把新錄音併進既有聲紋（跨場、跨通道越多越穩）
uv run speaker.py show
```

設定優先序：命令列參數 > 設定檔 > 程式內建預設。設定檔的找法：`--config <檔>` > 環境變數 `TRANSCRIBE_CONFIG` > 腳本旁的 `core.toml`。

## 特點

- **反幻覺過濾**：偵測重複段、清除字幕組招呼語、移除開頭提示詞回吐。溫度回退機制刻意保留
  （傳單一溫度 0.0 會關掉整個機制，靜音處就會無限重複）
- **詞組要少**：預設只用 `core`，這場真正需要的 3～5 個詞用 `--extra` 補。理由與實測見 `keywords.py` 開頭
- **中文輸出一律轉繁體**：每個 30 秒窗只靠提示詞維持繁體，有些錄音會整段掉回簡體（實測約 13%），
  調提示詞壓不掉，所以輸出前用 OpenCC `s2tw` 轉一次。只轉字形不轉詞彙；`台`／`了`／`只` 保留原字
  （s2tw 會轉成臺／瞭／隻，實測是誤判）。`--no-s2tw` 關掉
- **同一檔重跑結果相同**：溫度回退是隨機抽樣，回退那一窗的文字一變，後面切窗位置跟著變、整段分岔。
  每個檔案開跑前固定亂數種子（`SAMPLING_SEED`），實測同一檔重跑 token 逐段一致，批次順序也不影響
- **語者標記不訓練模型**：聲紋是本人幾段乾淨語音的 embedding 平均值；判定門檻由每份錄音自己導出，
  不同通道（電話 8kHz／實體面試）不必重調
- **錄音的頻寬上限在上游**：手機通話的對方語音在電信網路就已經被壓成窄頻，48kHz 也救不回來。
  細節見 `record.py` 開頭

## 不進版控

`audio/`、`out/`、`bench/`、各種音檔副檔名、聲紋（`speaker/enroll/`）、語者標記模型（`speaker/models/`），
以及手寫的標記檔（`*.tsv`，註解常寫著公司與對方姓名；只有範例 `speaker/labels.example.tsv` 例外）。

## 依賴

宣告在 `pyproject.toml`、鎖在 `uv.lock`（mlx-whisper、sherpa-onnx、opencc-python-reimplemented）。另外需要 `ffmpeg`（`brew install ffmpeg`）。
