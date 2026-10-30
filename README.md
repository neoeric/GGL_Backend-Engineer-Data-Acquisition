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

`--model none --fields post_text continuations` 為明確指定欄位的零模型模式，不需任何金鑰。`--seconds` 可縮短預設 120 秒期限，上限也是 120 秒。

離線執行、測試與格式檢查（mock 是 stub，沒有真實模型呼叫）：

```powershell
.\.venv\Scripts\python.exe -m threads_agent --model mock --fixture tests/fixtures/thread.json --message '全部 https://www.threads.com/@fixture_author/post/ROOT' --output result.json --trace trace.json
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pip install black ruff
.\.venv\Scripts\python.exe -m black --check src tests
.\.venv\Scripts\python.exe -m ruff check src tests
```

## 輸出

stdout 僅 JSON，診斷走 stderr。`data` 只含指定欄位：`display_name`、`follower_count`、`post_text`、`continuations`。

| 整次 `status` | 退出碼 | 意義 |
| --- | --- | --- |
| `complete` | 0 | 所有欄位 `ok`、目標身分已核對、模型用量完整、沒有 issue |
| `partial` | 3 | 至少一欄有值，但有欄位不完整、有 issue 或有技術性失敗 |
| `unavailable` | 3 | 沒有任何欄位取得值，且不是技術錯誤 |
| `invalid_request` | 2 | URL 數量／範圍、欄位或預算參數無效 |
| `error` | 1 | 技術性失敗（模型、瀏覽器、結果結構、寫檔） |

欄位層級 `status` 為 `ok`、`partial`、`unconfirmed`、`unavailable`、`error`；非 `ok` 必附 `reason_code` 與 `reason`。`follower_count` 保留顯示精度；縮寫數量另列 `approximate` 估值、`exact=null`。每份結果附來源 URL、canonical URL、轉址鏈、UTC 擷取時間 `captured_at` 與整次模型 token；未知用量為 `null`，零模型呼叫才報零。

## 設計

流程為「解析需求 → 必要頁面擷取 → 判斷 → 輸出」。模型只解析一次 URL 與欄位；URL 另由程式獨立擷取並要求與模型輸出一致，模型無法改寫目標。程式只讀取需要的貼文、續文與作者頁；原文由工具取得，保留順序，不摘要或由模型改寫。

| 模組 | 職責 |
| --- | --- |
| `__main__.py` | 參數解析、退出碼、原子寫檔 |
| `agent.py` | 有界代理迴圈：解析驗證、工具排程、fixture 瀏覽器 |
| `model.py` | OpenAI-compatible JSON 呼叫、用量計數、回應證據去密、mock |
| `browser.py` | 匿名 Chromium、CDP 文件回應攔截、資源政策、三個擷取工具 |
| `extraction.py` | 公開 JSON 與可見 DOM 的證據萃取、續文分類、觀察合併 |
| `security.py` | URL 範圍、公網 DNS 檢查、轉址守衛 |
| `core.py` | 結果契約與驗證、預算、trace 去密、原子寫檔 |

**來源核對。** 先核對目標 permalink／作者，再讀公開 JSON 或可辨識的可見本文。正文可優先選用同一可見時間戳 permalink 下的單一正文容器；不抓整個 body，保留與 caption 的差異，裁切時維持 partial。不同公開 JSON script 的身分與 self_thread 片段，僅以已觀察且唯一一致的 post code／pk／id 核對容器；未知或矛盾參照不連到目標。

**續文判定。** 續文必須是目標作者的貼文，且能沿父子關係回到目標貼文。父關係證據依序為：

1. 公開 JSON 明確的 `reply_to_post.code`，或可見對話區的明確回覆連結。
2. 已核對容器的 `self_thread.posts.edges[0]`，另需 `is_reply=true` 且 `reply_to_author` 為作者。
3. `self_thread` 第 2 個節點起，公開 JSON 不再提供 `reply_to_post`。此時以前一個節點為父，標記 `relationship_inferred="self_thread_sequence"`，續文保留但欄位維持 `partial`。明確的 `reply_to_post` 若與推定相符則視為確認、不同則標 `evidence_conflict`，節點缺 code 則停止推定。修正前這些節點被歸為 `ambiguous_ancestry` 整批丟棄，4 則的作者串只回 1 則。

排除其他人留言、作者回覆讀者（含其後的再回覆）、`linked_inline_media` 與 `relatedPosts` 推薦內容；同作者或畫面相鄰不足以證明續文。順序只來自唯一的父子鏈，不用 DOM 或序列化順序。未觀察續文區域時回 `null`；有更多按鈕、截斷或脈絡歧義時保留已確認內容並說明缺漏。

**瀏覽器邊界。** 每次建立新的匿名瀏覽器 context；主文件由 Chromium 原生載入，HTTP 轉址在回應階段逐跳檢查，HTTP／meta／JS 三種導航共用 5 跳預算與迴圈偵測，保留官方 HTTPS 網址、私人位址拒絕、非 GET 與 iframe 阻擋、單一資源 8 MB 與 150 個資源上限。不繞過登入牆、403／429 或驗證碼；非遮擋內容的登入邀請不等於登入牆。trace 保存模型／工具輸入、結果與結束狀態，排除金鑰、headers、cookies 與私有思考；需要遮罩原文時標示 partial／sanitized。

## 耗時量測

2026-10-30 於 Windows 11 開發機、家用網路，零模型模式、四欄位全要，每個 URL 各起一個 CLI 行程、間隔 3 秒。端到端為父行程量到的整個 CLI 牆鐘時間；工具欄位取自 trace 的 `elapsed_ms`，`read_post` 含啟動 Chromium（另量約 0.9 秒）。原始 result／trace 與 `summary.json` 在 `examples/timing-20261030/`。

| # | 貼文 | 端到端 | read_post | expand_thread | read_profile | 狀態 | 續文 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `@whoscall_tw/post/Dd0_RGwH4K7` | 9.9 s | 5.8 s | 0.5 s | 3.0 s | partial | 1 |
| 2 | `@whoscall_tw/post/Dd78uPeHwkx` | 10.3 s | 3.9 s | 0.1 s | 5.6 s | partial | 1 |
| 3 | `@whoscall_tw/post/DeYW3X0Hydc` | 5.8 s | 2.9 s | 0.4 s | 1.9 s | partial | 3 |
| 4 | `@whoscall_tw/post/DfAO62PD9np` | 7.8 s | 2.7 s | 0.1 s | 4.3 s | partial，本文 partial | 2 |
| 5 | `@zuck/post/Dd1MqeVG3vT` | 6.7 s | 3.0 s | 0.4 s | 2.6 s | partial | 2 |
| 6 | `@zuck/post/DeZgeQhm6RW` | 6.3 s | 3.1 s | 0.1 s | 2.4 s | partial，續文 unconfirmed | 0 |
| 7 | `@whoscall_tw/post/Dd0_RGwH4K7`（再跑） | 5.2 s | 2.9 s | 0.1 s | 1.6 s | partial | 1 |

七次 HTTP 皆 200，沒有被擋或錯誤。`read_profile` 1.6 到 5.6 秒變異最大，原因未查明。較早保留的三次執行（見下表）為 8.9、14.0、14.3 秒；有接模型那次的模型解析佔 4.4 秒。機器與網路不同，數字不可直接相比。

## 驗證與範例

134 項離線測試通過，涵蓋四欄 15 種組合、原文順序、作者續文與讀者分支排除、self_thread 序列推定與衝突、原文完整性、縮寫粉絲數、缺漏、網址／轉址、分段身分核對、模型用量／失敗及去密。測試使用合成回應與公開記錄重組 fixture，均與真實平台執行分開；重組 fixture 不代表原始 script 拓撲。

| 目錄 | 執行方式與結果 |
| --- | --- |
| `examples/minimal/` | 合成 fixture＋mock；展示四欄輸出，非真實模型或平台執行。 |
| `examples/share/` | 2026-10-29 分享 URL 的匿名零模型執行；HTTP 轉址核對到指定 Whoscall 貼文，結果 partial，0 tokens。 |
| `examples/whoscall-e2e/` | 2026-10-29 真實自然語言 CLI → DeepInfra/Llama → 匿名取料；一次模型呼叫 145 input＋58 output＝203 tokens、usage_complete=true；結果 partial。 |
| `examples/whoscall-fragment-live/` | 2026-10-29 匿名四欄零模型執行；作者 Whoscall、粉絲數 15,105、四行主貼 ok，一則作者續文及父關係；附去密關係證據。 |
| `examples/timing-20261030/` | 2026-10-30 七次零模型執行，續文判定修正後的程式；含耗時 `summary.json`。 |

指定來源：`https://www.threads.com/@whoscall_tw/post/Dd0_RGwH4K7`。前三組 live 範例早於續文判定修正，彼此是不同次執行，沒有拼接成四欄全部完整的模型執行。每份 result 保留各自來源、`captured_at`、整次 token 用量與缺漏原因，trace 保存呼叫／結果／run_end。公開範例僅將 trace 輸出位置改為相對路徑，並移除 URL 的 xmt 追蹤參數。

## 已知限制

- 不捲動、不點「更多」：續文只來自頁面初次 hydration 可見的公開 JSON 與 DOM，`expand_thread` 只是等待後再讀一次。長串未載入的部分不會取得，`has_more` 為真時標 `thread_incomplete`。
- 作者在同一則下直接回兩則以上（分叉）時，父子鏈不唯一，無法指派順序，續文回 `unconfirmed`／`null`。
- 自然語言中 URL 與中文黏在一起（如「…post/ABC的內容」）會被當成網址一部分而判為 `unsupported_url`；URL 前後請留空白。
- `source_url` 保留原始 query；公開範例中的 xmt 追蹤參數是手動移除。
- `captured_at` 為最後一個成功工具的時間，讀取作者頁後會覆蓋貼文擷取時間。
- 頁面結構與匿名可見範圍可能改變；不足以確認的資料會具體說明，不假造或突破平台限制。強制終止或斷電不保證最後寫檔。

## reason_code 索引

| 類別 | 代碼 |
| --- | --- |
| 輸入無效 | `unsupported_url` `invalid_url_count` `unsupported_or_missing_fields` `explicit_fields_required` `invalid_limits` |
| 模型 | `model_configuration_missing` `model_configuration_invalid` `model_disabled` `model_http_error` `model_schema_error` `model_url_mismatch` `model_incomplete_response` `model_response_error` `llm_budget_exceeded` `usage_incomplete` `unsupported_model_operation` |
| 導航與範圍 | `redirect_target_blocked` `redirect_loop` `redirect_limit_exceeded` `resolution_timeout` `dns_error` `unresolved_target` `document_guard_error` |
| 平台回應 | `login_required` `access_denied` `rate_limited` `login_or_challenge` `share_unavailable` `source_unavailable` `source_too_large` |
| 瀏覽器技術 | `browser_navigation_error` `browser_network_error` `browser_observation_error` `browser_close_error` |
| 預算與工具 | `time_budget_exceeded` `tool_budget_exceeded` `tool_timeout` `tool_error` `tool_precondition` `fixture_target_mismatch` |
| 證據品質 | `hydration_unconfirmed` `acquisition_policy_limited` `evidence_target_mismatch` `source_evidence_conflict` `thread_incomplete` `thread_context_unconfirmed` `text_completeness_unconfirmed` `profile_identity_unconfirmed` `source_text_sanitized` |
| 欄位缺漏 | `display_name_missing` `follower_count_missing` `follower_format_unknown` `post_text_missing` `not_observed` |
| 結果與輸出 | `result_schema_error` `output_write_error` `unexpected_error` |

trace 的 `acquisition_diagnostics.counts` 另有資源層級代碼：`policy_non_get_blocked` `policy_frame_blocked` `policy_resource_budget_or_type` `policy_resource_redirect_blocked` `policy_resource_size_blocked` `http_redirect_reissued` `request_failed` `resource_http_error` `resource_transport_error` `page_script_error` `run_stopped`。

## AI 使用

Codex 協助設計、實作、測試與文件。離線測試與模型解析成功不代表所有來源欄位完整。強制終止或斷電不保證最後寫檔。
