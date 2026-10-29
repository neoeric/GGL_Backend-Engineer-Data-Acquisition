# Threads acquisition CLI

CLI 接收自然語言中的單一 Threads 分享／正式 URL 與欄位，使用獨立未登入 Chromium 擷取，輸出 JSON 與去密 trace。

## 安裝與執行

Python 3.11+，依賴安裝於專案環境：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e . --no-deps --no-build-isolation
.\.venv\Scripts\python.exe -m playwright install chromium
$env:LLM_BASE_URL = 'https://api.deepinfra.com/v1/openai'
$env:LLM_MODEL = 'meta-llama/Llama-3.3-70B-Instruct-Turbo'
# 私下安全設定 LLM_API_KEY，勿把真 key 寫入程式或命令歷史。
.\.venv\Scripts\python.exe -m threads_agent --message '請取得作者個人檔案名稱、追蹤者數、貼文內容及作者續文：https://www.threads.com/share/GW3EPb5M8/' --output result.json --trace trace.json
```

使用 DeepInfra 的 OpenAI-compatible JSON 介面與 Meta Llama-3.3-70B-Instruct-Turbo，模型僅解析自然語言需求。[官方 API 文件](https://deepinfra.com/meta-llama/Llama-3.3-70B-Instruct-Turbo/api)。`.env.example` 僅是占位範例；程式不會自動載入它或 `.env`。請明確設定 base／model；未設定 base 時預設為 `https://api.openai.com/v1`。

`data` 只含指定欄位：`display_name`、`follower_count`、`post_text`、`continuations`。stdout 僅 JSON，診斷走 stderr。退出碼：0 完成、2 輸入無效、3 部分／不可取得、1 技術／保存失敗。`--seconds` 可縮短預設 120 秒期限。`--model none --fields post_text continuations` 為明確指定欄位的零模型模式。

離線執行與測試（mock 是 stub，沒有真實模型呼叫）：

```powershell
.\.venv\Scripts\python.exe -m threads_agent --model mock --fixture tests/fixtures/thread.json --message '全部 https://www.threads.com/@fixture_author/post/ROOT' --output result.json --trace trace.json
.\.venv\Scripts\python.exe -m pytest
```

## 設計

流程為「解析需求 → 必要頁面擷取 → 判斷 → 輸出」。模型解析一次 URL 與欄位；程式只讀取需要的貼文、續文與作者頁。原文由工具取得，保留順序，不摘要或由模型改寫。

先核對目標 permalink／作者，再讀公開 JSON 或可辨識的可見本文。續文依明確回覆關係或可見對話連結判斷，排除其他人留言、作者回覆讀者與推薦內容；同作者或畫面相鄰不足以證明續文。同身份、同文字 JSON 缺 parent 時補入 DOM 明確回覆連結，衝突維持歧義。正文可優先選用同一可見時間戳 permalink 下的單一正文容器；不抓整個 body，保留與 caption 的差異，裁切時維持 partial。對已觀察的根貼 self_thread.posts.edges 首節點，另核對 is_reply 與 reply_to_author 才建立父關係；不只憑 self_thread 標籤或作者相同。不同公開 JSON script 的身分與 self_thread 片段，僅以已觀察且唯一一致的 post code／pk／id 核對容器；未知或矛盾參照不連到目標。trace 保留候選來源 path、回覆欄位與 self_thread context。讀者 direct_replies 分支及 linked_inline_media／relatedPosts 均排除。未觀察續文區域時回 null；有更多按鈕、截斷或脈絡歧義時保留已確認內容並說明缺漏。

追蹤者保留顯示精度；縮寫數量另列估值，exact=null。附來源 URL、UTC 擷取時間、每欄狀態／原因與整次模型 token；未知用量為 null，零模型呼叫才報零。

每次建立新的匿名瀏覽器 context；主文件由 Chromium 原生載入，HTTP 轉址在回應階段逐跳檢查，保留官方 HTTPS 網址、私人位址拒絕、有限轉址／逾時與資源限制。不繞過登入牆、403／429 或驗證碼；非遮擋內容的登入邀請不等於登入牆。trace 保存模型／工具輸入、結果與結束狀態，排除金鑰、headers、cookies 與私有思考；需要遮罩原文時標示 partial／sanitized。

## 驗證與限制

125 項離線測試通過，涵蓋四欄 15 種組合、原文順序、作者續文與讀者分支排除、原文完整性、縮寫粉絲數、缺漏、網址／轉址、分段身分核對、模型用量／失敗及去密。測試使用合成回應與公開記錄重組 fixture，均與真實平台執行分開；重組 fixture 不代表原始 script 拓撲，另有分段與衝突身分回歸。

保留四組不同次執行的 result／trace：

| 目錄 | 執行方式與結果 |
| --- | --- |
| `examples/minimal/` | 合成 fixture＋mock；展示四欄輸出，非真實模型或平台執行。 |
| `examples/share/` | 分享 URL 的匿名零模型執行；HTTP 轉址核對到指定 Whoscall 貼文，當時結果 partial，0 tokens。 |
| `examples/whoscall-e2e/` | 真實自然語言 CLI → DeepInfra/Llama → 匿名取料；一次模型呼叫，145 input＋58 output＝203 tokens、usage_complete=true；當時資料結果 partial。 |
| `examples/whoscall-fragment-live/` | 最新匿名四欄零模型執行；作者 Whoscall、粉絲數 15,105、四行主貼均 ok，已保留一則作者續文及父關係；續文文字完整性未聲明，欄位與整次 partial，0 tokens、usage_complete=true。附必要去密關係證據。 |

指定來源：`https://www.threads.com/@whoscall_tw/post/Dd0_RGwH4K7`。最新 UTC 擷取時間為 `2026-10-29T08:22:39.868Z`。根貼完整身分與 self_thread 片段在不同公開 scripts，以相同精確 post ID 核對；read_post／expand_thread 各確認一個 self_thread 子節點，補充原文與父關係均保留。讀者提問、作者回覆讀者及引用內容均排除；不以同作者、畫面相鄰或留言數推定續文。

這些是不同次執行，真實模型例與最新零模型來源驗證分開呈現，沒有拼接成四欄全部完整的模型執行。每份 result 保留各自來源、UTC 擷取時間 captured_at、整次 token 用量與缺漏原因，trace 保存呼叫／結果／run_end；未知用量回 null，不冒充 0。公開範例僅將 trace 輸出位置改為相對路徑，並移除 URL 的 xmt 追蹤參數。

頁面結構與匿名可見範圍可能改變。續文原文已取得，但其文字完整性及未載入內容仍未確認，因此保留 partial；不足以確認的資料會具體說明，不假造或突破平台限制。

## AI 使用

Codex 協助設計、實作、測試與文件。離線測試與模型解析成功不代表所有來源欄位完整。強制終止或斷電不保證最後寫檔。
