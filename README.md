# ModCrash AI V2

ModCrash AI 是一個遊戲模組相容性分析平台，支援玩家輸入模組名稱或上傳模組列表，並依不同遊戲分類進行衝突分析。

## Features

- 多遊戲模組相容性分析
- 帳號註冊 / 登入
- Free / Basic / Pro / Creator 方案
- 每日分析次數限制
- 資料庫快速分析
- OpenAI 深度分析
- 衝突資料庫
- 安全組合資料庫
- 待驗證組合觀察機制
- PostgreSQL 資料儲存
- Render 部署支援

## Data Logic

- ConflictRule：已知衝突組合
- SafeCombination：玩家明確回報正常或示範資料
- UnknownObservation：系統觀察到但尚未確認安全或衝突的待驗證組合

當未知組合累積到 AUTO_SAFE_THRESHOLD 次，且資料庫內沒有相同組合的衝突紀錄時，系統會自動以 `auto_candidate` 來源寫入 SafeCombination。

## Render Environment Variables

```text
DATABASE_URL=Render Internal Database URL
OPENAI_API_KEY=your OpenAI API key
OPENAI_MODEL=gpt-4.1-mini
PYTHON_VERSION=3.11.9
SECRET_KEY=your secret key
AUTO_SAFE_THRESHOLD=5
UNKNOWN_OBSERVE_LIMIT=100
MAX_CONFLICT_RESULTS=80
MAX_SAFE_RESULTS=80
MAX_UNKNOWN_RESULTS=30
```

## Start Command

```bash
gunicorn app:app
```


## OpenAI Database Writeback

OpenAI 深度分析會保存兩種資料：

1. RawReport：完整 AI 分析紀錄，一定會保存。
2. ConflictRule：只有 AI 回傳 `likely_conflicts` 內的衝突組合才會寫入公開衝突資料庫。

如果 AI 沒有回傳明確衝突，公開資料庫不會新增衝突資料，但原始 AI 報告仍會保存。


## Auto Safe Promotion

本版本保留自動升級安全機制：

```text
未知組合多次出現在玩家模組列表
↓
observe_count >= AUTO_SAFE_THRESHOLD
↓
若沒有已知衝突紀錄
↓
寫入 SafeCombination，source=auto_candidate
```

此機制代表「高頻共現候選安全」，不是絕對保證安全。


## Unknown Observation Counter Fix

待驗證組合數代表「尚未升級、尚未確認衝突的唯一模組配對數」，不是使用次數。

本版本新增：
- `unknown_pending_count`：仍待驗證的唯一配對數
- `unknown_promoted_count`：已自動升級的配對數
- `unknown_total_count`：所有曾觀察到的唯一配對數
- `unknown_observation_total`：累積觀察次數總和

大型模組列表不再永遠只記錄前 100 組未知配對，而是隨機抽樣 UNKNOWN_OBSERVE_LIMIT 組，讓重複分析可以逐步累積不同配對。


## OpenAI Safe Writeback

本版本支援 OpenAI 安全組合回寫：

- `likely_conflicts` → 寫入 `ConflictRule`，source=`openai`
- `likely_safe_combinations` → 寫入 `SafeCombination`，source=`openai_safe`
- 若同一配對已存在於 `ConflictRule`，不會被寫入安全組合，避免衝突資料被覆蓋成安全。
- AI 原始回覆仍會保存到 `RawReport`。
