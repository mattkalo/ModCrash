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

未知組合不會自動升級成安全組合；必須由玩家明確回報正常才會進入安全資料庫。

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
