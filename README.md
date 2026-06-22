# ModCrash AI V2 Clean Final

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
- PostgreSQL 資料儲存
- Render 部署支援

## Data Logic

公開頁面只顯示兩種正式資料：

1. `ConflictRule`：已知衝突組合
2. `SafeCombination`：安全組合資料庫

內部仍保留 `UnknownObservation`，但它只用於自動安全累積，不再顯示在公開資料庫頁面。

## Safety Sources

`SafeCombination.source` 可能為：

- `user`：玩家手動回報正常
- `demo`：示範資料
- `auto_candidate`：系統根據多次觀察自動升級
- `openai_safe`：OpenAI 明確回傳 `likely_safe_combinations`

## OpenAI Writeback

OpenAI 深度分析會保存：

1. `RawReport`：完整 AI 分析紀錄
2. `ConflictRule`：只有 AI 回傳 `likely_conflicts` 時才寫入
3. `SafeCombination`：只有 AI 回傳 `likely_safe_combinations` 時才寫入

## Auto Safe Promotion

系統仍保留自動升級安全機制：

```text
未知配對多次出現在玩家模組列表
↓
observe_count >= AUTO_SAFE_THRESHOLD
↓
若沒有已知衝突紀錄
↓
寫入 SafeCombination，source=auto_candidate
```

`UnknownObservation` 只做內部累積，不顯示於公開資料庫。

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
