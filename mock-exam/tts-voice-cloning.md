# 用自己的聲音產題目音檔 —— BreezyVoice 錄音與使用筆記

> **狀態：Future Works（研究筆記，未接進 mock.py）**。`mock.py` 目前用 macOS `say`；本檔是評估 BreezyVoice 時的紀錄，repo 裡沒有附對應的程式與 demo 音檔。
> 下面的 `single_inference.py` 是 [BreezyVoice](https://github.com/mtkresearch/BreezyVoice) 官方 repo 裡的腳本，要在那個 repo 裡執行。
> 目的:取代 `mock.py` 現在用的 macOS `say -v Meijia`,讓模擬面試的題目
> 音檔聽起來像真人。

## 先搞清楚:不是 train,是 clone

BreezyVoice 是**零樣本克隆**。你的聲音不會變成模型權重,而是**推論時的一個輸入條件**。

| | 傳統 fine-tune | 零樣本克隆 |
|---|---|---|
| 要多少語音 | 幾十分鐘到數小時 | **5~10 秒** |
| 要多久 | GPU 數小時 | **0 秒**,直接推論 |
| 產出 | 一份新權重 | 什麼都沒產生,每次推論現場給參考音檔 |
| 換聲音 | 重訓 | 換一個 wav 檔 |

## 🔴 先決定:要用誰的聲音

**用自己的聲音當面試官會很怪。** 模擬面試的價值在於進入「被陌生人質問」的狀態,
自己的聲音問自己問題,那個壓力感會消失。

比較合理:找一個**順耳、而且願意授權**的真人(朋友、家人、前同事),錄十秒。
腔調自然是台灣的,聲音不討厭,而且不是自己。

克隆他人聲音的界線在**同意**。自己的聲音最乾淨,他人的要先問過。

---

## 錄音要點(照影響力排序)

⚠ BreezyVoice 的 README **沒有規定**參考音檔規格(長度、取樣率都沒寫)。
下面是通用實務與 CosyVoice 系的慣例,**不是官方要求**,最佳值要自己 A/B。

1. **🔴 不要用藍牙麥克風。** 藍牙一啟用麥克風就切到通話協定(HFP/mSBC),
   頻寬砍到 16 kHz 且重壓縮,聽起來像電話 —— 那個壓縮痕跡會被完整克隆。
   用 MacBook 內建麥(avfoundation `:4`)或任何有線麥都遠勝藍牙耳機。

2. **🔴 房間殘響比雜訊更致命。** 穩定底噪(冷氣、冰箱)模型還有機會忽略,
   **殘響會被當成音色**克隆進去,之後每一句都帶著那個房間。
   挑小房間、有軟裝的角落(床邊、衣櫃前、掛著外套的地方),不要空曠客廳或浴室。

3. **長度 5~10 秒是甜蜜點。**
   - 太短(< 3 秒):音色抓得到,語調與節奏資訊不夠,合成出來很平
   - 太長(> 15~20 秒):通常沒有更好,部分實作直接截斷前段
   - 重點是**密度**:十秒內要有完整的句子起伏,中間不要長停頓,結尾講完整個詞再停

4. **參考音檔的語調 = 輸出的語調。** 錄得快就唸得快,錄得懶散就懶散。
   要的是「面試官提問」的語氣,就**用提問的語氣錄**,不要用聊天的語氣。

5. **單聲道。** 模型的說話人編碼器只吃單聲道,給雙聲道一定會降混。
   問題在誰來降、怎麼降 —— 兩個真的不同的聲道相加會產生**梳狀濾波**,
   某些頻率互相抵消,聲音變空變薄,那個空洞感會被克隆進去。
   錄的時候就錄一軌,是為了**消掉一個控制不了的步驟**,不是因為單聲道比較好聽。

6. **電平留餘裕。** 峰值抓 -6 ~ -3 dBFS。離麥克風一個拳頭,而且**稍微偏角度**
   —— 正對著唸 ㄆ、ㄅ 開頭的字會噴麥,低頻爆點事後修不掉。

7. **🔴 不要做後製。** 降噪、EQ、壓縮全部不要,特別是降噪 ——
   激進降噪會製造水聲般的音樂雜訊,模型會一起學走,比原本的底噪更難聽。
   **錄壞了就重錄,不要修。**

8. **原生取樣率錄,事後用 soxr 降。** 差異不大,但免費。

9. **頭尾靜音剪掉**,前後各留 0.1~0.2 秒。

10. **🔴 最有效的一招:錄五次,選一次。**
    同一段話錄五個 take,每個丟進去合成同一句測試句,盲聽挑最好的當固定參考。
    **選對參考音檔的效益遠大於調任何參數**,成本只有五分鐘。

### 建議的錄音內容

隨便講什麼都可以,但**最好跟目標語域接近**。之後要唸的是技術面試題,
那參考音檔就講一句帶技術詞的話,例如:

> 我平常用 Ansible 做組態管理,服務部署在 Kubernetes 上。

---

## 步驟

### ① 錄

```bash
# 原生 48 kHz、單聲道、10 秒。:4 是 MacBook Pro Microphone，
# 索引會隨藍牙裝置位移，先用 mock.py 的選單或 ffmpeg -list_devices 確認。
ffmpeg -f avfoundation -i ":4" -ar 48000 -ac 1 -t 10 ~/Desktop/myvoice-48k.wav
```

### ② 檢查電平(決定要不要重錄)

```bash
ffmpeg -hide_banner -i ~/Desktop/myvoice-48k.wav -af volumedetect -f null - 2>&1 \
  | grep -E 'max_volume|mean_volume'
```

`max_volume` 到 0.0 dB = 已削頂,重錄。落在 -6 ~ -3 dB 最理想。

### ③ 降到 24 kHz

```bash
ffmpeg -i ~/Desktop/myvoice-48k.wav \
  -af "aresample=24000:resampler=soxr:precision=28" ~/Desktop/myvoice.wav
```

### ④ 單句測試

```bash
uv run python single_inference.py \
  --speaker_prompt_audio_path ~/Desktop/myvoice.wav \
  --speaker_prompt_text_transcription "我平常用 Ansible 做組態管理，服務部署在 Kubernetes 上" \
  --content_to_synthesize "你好，我是今天的面試官。可以請你先簡單自我介紹一下嗎？" \
  --output_path ./test.wav
```

逐字稿是選填,不給會自動用 Whisper 轉 —— **但一定要手動給**。
轉錯一個字就影響對齊品質,而 Whisper 會把英文縮寫聽錯(實測過把證照縮寫聽成一個英文單字)。

🔴 **測試句一定要含英文術語**(例如 kubectl、Prometheus、etcd)。
中英混讀正是換掉 Kokoro 的理由,不驗這個等於沒驗。

### ⑤ 批次產題目音檔

`batch_inference.py` 吃 CSV,欄位:

```
speaker_prompt_audio_filename, speaker_prompt_text_transcription,
content_to_synthesize, output_audio_filename
```

```bash
uv run python batch_inference.py --csv_file ./data/batch_files.csv \
  --speaker_prompt_audio_folder ./data \
  --output_audio_folder ./results
```

這跟 `mock.py` 的 `prerender()` 一對一對得上,題庫八題直接轉成八列 CSV。

---

## 接進 mock.py

改動點只有一處:`prerender()` 裡呼叫 `say -v <voice> -r <rate> -o <out>` 那一行
(約 `mock.py:109`)。題目音檔本來就是**算一次快取重複用**的設計,
所以換後端不影響其他流程。

建議加 `--tts breezy|say` 開關,**`say` 留作退路**。

---

## 環境與現況

- **模型**:`MediaTek-Research/BreezyVoice`,Apache-2.0,3.2 GB / 154 檔
  - 預設下載位置 `~/.cache/huggingface/hub/models--MediaTek-Research--BreezyVoice`
    (實際位元組在 `~/.cache/huggingface/hub/blobs`,兩個都要備份,搬家用 `rsync -a` 保留 symlink)
- **來源**:聯發科研究院 + 台大,基於 CosyVoice,arXiv:2501.17790
  - 台灣中文、**破音字處理**、code-switching 有專門評測
- ⚠ **README 只提 CUDA 與「runnable on CPU」,沒提 Apple Silicon / MPS**
  → 在 Mac 上預期是 CPU 推論,會慢。但 prerender 是一次性成本,可接受。
- ⚠ 需 **Python 3.10**,相依走 onnxruntime(專案規範一律用 uv 管理)

## 為什麼不用 Kokoro(已實測並否決)

`prince-canuma/Kokoro-82M` + `mlx-audio` + `misaki[zh]`,MLX 後端。
實測 RTF 0.51x、峰值記憶體 1.68 GB、`HF_HUB_OFFLINE=1` 可完全離線,**音質遠勝 `say`**。

否決的三個理由:

1. 中文只有 8 個 Azure zh-CN 系語音(`zf_xiaobei` / `xiaoni` / `xiaoxiao` / `xiaoyi`、
   `zm_yunjian` / `yunxi` / `yunxia` / `yunyang`),**沒有 zh-TW**,聽起來是中國腔
2. **沒有 pitch 參數**,只有 speed。事後用 ffmpeg `asetrate` + `atempo` 變調會
   連共振峰一起搬,降調有桶音、升調有花栗鼠感
3. 🔴 **句中英文術語會被 misaki 的中文 G2P 唸爛**(工具名、檔案系統名這類
   專有名詞)—— 技術題庫幾乎每句都有這類字,等於堵死

對照之下,現況的 `say -v Meijia` 其實**腔調是對的**(Mei-Jia 是 zh-TW),
輸在音質。Kokoro 是音質好、腔調錯,沒有贏過現況。
