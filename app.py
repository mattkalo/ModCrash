from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from itertools import combinations
from datetime import datetime, date
from openai import OpenAI
import os
import re
import json

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this")

# =========================
# Database Config
# =========================

database_url = os.environ.get("DATABASE_URL")

if database_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url.replace("postgres://", "postgresql://")
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///modcrash_v2.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
AUTO_SAFE_THRESHOLD = int(os.environ.get("AUTO_SAFE_THRESHOLD", "5"))

# =========================
# Game Profiles
# =========================

GAME_PROFILES = {
    "skyrim": {
        "label": "Skyrim / Skyrim Special Edition",
        "badge": "🏔️",
        "theme": "Northern Archive",
        "extensions": [".esp", ".esm", ".esl"],
        "examples": ["SkyUI", "XPMSSE", "Serana Dialogue Add-On"]
    },
    "fallout4": {
        "label": "Fallout 4",
        "badge": "⚙️",
        "theme": "Wasteland Bureau",
        "extensions": [".esp", ".esm", ".esl"],
        "examples": ["LooksMenu", "Sim Settlements 2", "Full Dialogue Interface"]
    },
    "stardew": {
        "label": "Stardew Valley",
        "badge": "🌾",
        "theme": "Valley Council",
        "extensions": [".dll", ".json", ".zip"],
        "examples": ["SMAPI", "Content Patcher", "Stardew Valley Expanded"]
    },
    "minecraft": {
        "label": "Minecraft",
        "badge": "🧊",
        "theme": "Block Federation",
        "extensions": [".jar", ".zip"],
        "examples": ["Fabric API", "Sodium", "Create"]
    },
    "cyberpunk": {
        "label": "Cyberpunk 2077",
        "badge": "🌆",
        "theme": "Night City Accord",
        "extensions": [".archive", ".xl", ".reds", ".lua"],
        "examples": ["Cyber Engine Tweaks", "ArchiveXL", "TweakXL"]
    },
    "generic": {
        "label": "其他遊戲 / 通用",
        "badge": "🌐",
        "theme": "Open Mod Union",
        "extensions": [],
        "examples": ["Mod A", "Mod B", "Texture Pack"]
    }
}

# =========================
# SaaS Plans
# =========================

PLAN_LIMITS = {
    "free": {
        "name": "Free",
        "zh_name": "免費版",
        "price": "$0",
        "daily_analyze_limit": 3,
        "daily_ai_limit": 0,
        "max_mods": 20,
        "max_display_pairs": 80,
        "allow_ai": False,
        "description": "一般玩家快速檢查。"
    },
    "basic": {
        "name": "Basic",
        "zh_name": "基礎訂閱版",
        "price": "$3 / month",
        "daily_analyze_limit": 20,
        "daily_ai_limit": 5,
        "max_mods": 80,
        "max_display_pairs": 1000,
        "allow_ai": True,
        "description": "適合中度玩家與常用模組包。"
    },
    "pro": {
        "name": "Pro",
        "zh_name": "進階訂閱版",
        "price": "$7 / month",
        "daily_analyze_limit": 100,
        "daily_ai_limit": 25,
        "max_mods": 150,
        "max_display_pairs": 3000,
        "allow_ai": True,
        "description": "適合重度玩家與大型 load order。"
    },
    "creator": {
        "name": "Creator",
        "zh_name": "創作者版",
        "price": "$15 / month",
        "daily_analyze_limit": 300,
        "daily_ai_limit": 100,
        "max_mods": 300,
        "max_display_pairs": 10000,
        "allow_ai": True,
        "description": "適合模組作者、整合包維護者與團隊。"
    }
}

# =========================
# Database Models
# =========================

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    plan = db.Column(db.String(50), default="free")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class UsageLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    action_type = db.Column(db.String(50), nullable=False)  # analyze / ai_analyze
    used_at = db.Column(db.DateTime, default=datetime.utcnow)


class ConflictRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(80), default="skyrim")
    module_a = db.Column(db.String(255), nullable=False)
    module_b = db.Column(db.String(255), nullable=False)
    conflict_type = db.Column(db.String(120), default="Unknown Conflict")
    risk = db.Column(db.String(80), default="中度功能異常")  # legacy name, now stores conflict_degree
    report_count = db.Column(db.Integer, default=1)
    confidence_score = db.Column(db.Float, default=0.5)
    source = db.Column(db.String(50), default="user")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SafeCombination(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(80), default="skyrim")
    module_a = db.Column(db.String(255), nullable=False)
    module_b = db.Column(db.String(255), nullable=False)
    report_count = db.Column(db.Integer, default=1)
    confidence_score = db.Column(db.Float, default=0.5)
    source = db.Column(db.String(50), default="user")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ObservedPair(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(80), default="skyrim")
    module_a = db.Column(db.String(255), nullable=False)
    module_b = db.Column(db.String(255), nullable=False)
    observation_count = db.Column(db.Integer, default=1)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)


class RawReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(80), default="skyrim")
    user_id = db.Column(db.Integer, nullable=True)
    report_type = db.Column(db.String(50), nullable=False)
    content = db.Column(db.Text, nullable=False)
    ai_result = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# =========================
# Schema Helper
# =========================

def ensure_schema():
    db.create_all()
    inspector = inspect(db.engine)

    add_columns = {
        "conflict_rule": {
            "game": "VARCHAR(80) DEFAULT 'skyrim'",
            "source": "VARCHAR(50) DEFAULT 'user'"
        },
        "safe_combination": {
            "game": "VARCHAR(80) DEFAULT 'skyrim'",
            "source": "VARCHAR(50) DEFAULT 'user'"
        },
        "raw_report": {
            "game": "VARCHAR(80) DEFAULT 'skyrim'",
            "user_id": "INTEGER"
        }
    }

    with db.engine.begin() as conn:
        for table_name, columns_to_add in add_columns.items():
            if not inspector.has_table(table_name):
                continue
            existing_cols = [col["name"] for col in inspector.get_columns(table_name)]
            for col_name, col_def in columns_to_add.items():
                if col_name not in existing_cols:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}"))

# =========================
# Auth Helpers
# =========================

def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return User.query.get(user_id)


@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(),
        "plans": PLAN_LIMITS,
        "games": GAME_PROFILES,
        "active_plan": PLAN_LIMITS.get(current_user().plan, PLAN_LIMITS["free"]) if current_user() else PLAN_LIMITS["free"]
    }


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("請先登入帳號。", "warning")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

# =========================
# Plan / Usage Helpers
# =========================

def today_usage_count(user_id, action_type):
    start = datetime.combine(date.today(), datetime.min.time())
    return UsageLog.query.filter(
        UsageLog.user_id == user_id,
        UsageLog.action_type == action_type,
        UsageLog.used_at >= start
    ).count()


def check_usage_allowed(user, action_type):
    plan = PLAN_LIMITS.get(user.plan, PLAN_LIMITS["free"])

    if action_type == "analyze":
        limit = plan["daily_analyze_limit"]
    elif action_type == "ai_analyze":
        limit = plan["daily_ai_limit"]
    else:
        limit = 0

    used = today_usage_count(user.id, action_type)
    return used < limit, used, limit


def record_usage(user, action_type):
    db.session.add(UsageLog(user_id=user.id, action_type=action_type))
    db.session.commit()

# =========================
# Request Helpers
# =========================

def get_plan_for_user(user):
    return PLAN_LIMITS.get(user.plan, PLAN_LIMITS["free"])


def get_game_from_request():
    game = request.form.get("game", request.args.get("game", "skyrim")).lower().strip()
    return game if game in GAME_PROFILES else "generic"


def get_game_from_json(data):
    game = data.get("game", "skyrim").lower().strip()
    return game if game in GAME_PROFILES else "generic"

# =========================
# Mod Parsing
# =========================

def clean_mod_line(line):
    line = line.strip().replace("\ufeff", "").replace("*", "").strip()
    if not line:
        return ""

    for sep in ["#", "//"]:
        if sep in line:
            line = line.split(sep)[0].strip()

    line = re.sub(r"^[-•●▪]+\s*", "", line)
    line = re.sub(r"^\d+[\.\)]\s*", "", line)
    line = re.sub(r"^\[[A-Fa-f0-9\s]+\]\s*", "", line)
    line = re.sub(r"^FE\s+[A-Fa-f0-9]{3}\s+", "", line, flags=re.IGNORECASE)
    line = re.sub(r"^[A-Fa-f0-9]{2,3}\s+", "", line)

    for sep in ["\t", "|", ","]:
        if sep in line:
            line = line.split(sep)[0].strip()

    line = re.sub(r"\s+\((enabled|disabled)\)$", "", line, flags=re.IGNORECASE)
    line = re.sub(r"\s+\[(enabled|disabled)\]$", "", line, flags=re.IGNORECASE)
    return line.strip().strip("\"'")


def is_noise_line(line):
    if not line:
        return True
    lower = line.lower().strip()
    noise = {
        "plugins", "plugin list", "load order", "mod list", "active mods",
        "inactive mods", "enabled mods", "disabled mods", "crash log", "stack trace",
        "error", "warning", "----", "===="
    }
    return lower in noise or len(line) > 120


def normalize_mod_name(name):
    name = clean_mod_line(name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def extract_mods(text_input, game="generic"):
    mods = []
    profile = GAME_PROFILES.get(game, GAME_PROFILES["generic"])
    extensions = profile.get("extensions", [])

    for raw_line in text_input.splitlines():
        line = normalize_mod_name(raw_line)
        if is_noise_line(line):
            continue

        lower = line.lower()
        if extensions and any(lower.endswith(ext.lower()) for ext in extensions):
            mods.append(line)
            continue

        # Accept plain mod names, one per line.
        if 2 <= len(line) <= 80:
            if not any(mark in line for mark in ["。", "，", "；", "："]):
                mods.append(line)

    seen = set()
    result = []
    for mod in mods:
        key = mod.lower()
        if key not in seen:
            seen.add(key)
            result.append(mod)
    return result

# =========================
# Conflict Helpers
# =========================

def normalize_pair(a, b):
    a = normalize_mod_name(a)
    b = normalize_mod_name(b)
    return tuple(sorted([a, b], key=lambda x: x.lower()))


def calculate_confidence(count):
    return round(min(0.35 + count * 0.08, 0.98), 2)


def default_degree_by_type(conflict_type):
    t = (conflict_type or "").lower()
    if any(k in t for k in ["dependency", "missing", "loader", "version"]):
        return "啟動阻斷"
    if "skeleton" in t:
        return "高度崩潰風險"
    if any(k in t for k in ["script", "dialogue", "dialog"]):
        return "中度功能異常"
    if any(k in t for k in ["asset", "texture", "mesh", "map", "world"]):
        return "視覺 / 地圖異常"
    if "load order" in t:
        return "輕微覆蓋"
    return "中度功能異常"


def normalize_conflict_degree(value, conflict_type="Unknown Conflict"):
    if not value:
        return default_degree_by_type(conflict_type)

    value = value.strip()
    legacy = {
        "High": "高度崩潰風險",
        "Medium": "中度功能異常",
        "Low": "輕微覆蓋",
        "Unknown": "未知衝突"
    }
    valid = {
        "啟動阻斷", "高度崩潰風險", "中度功能異常", "視覺 / 地圖異常", "輕微覆蓋", "未知衝突", "無明顯衝突"
    }
    if value in legacy:
        return legacy[value]
    if value in valid:
        return value
    return default_degree_by_type(conflict_type)


def impact_description(conflict_type, degree):
    text_all = f"{conflict_type} {degree}".lower()
    if "dependency" in text_all or "missing" in text_all or degree == "啟動阻斷":
        return {"impact_area": "啟動 / 讀檔", "effect": "可能缺少前置模組或版本不相容，遊戲可能無法啟動、讀檔閃退，或在主選單前崩潰。"}
    if "loader" in text_all or "version" in text_all:
        return {"impact_area": "模組載入器 / 版本", "effect": "可能因遊戲版本、模組載入器或必要函式庫不同，造成啟動失敗或進入世界時崩潰。"}
    if "skeleton" in text_all:
        return {"impact_area": "角色骨架 / 動作", "effect": "遊戲可能可啟動，但角色可能出現 T-Pose、動作異常、戰鬥動畫錯誤，嚴重時會崩潰。"}
    if "script" in text_all:
        return {"impact_area": "任務 / 腳本", "effect": "遊戲通常可以執行，但任務可能卡住、功能不觸發、事件失效，長時間遊玩後可能出現存檔污染。"}
    if "dialogue" in text_all or "dialog" in text_all:
        return {"impact_area": "角色 / 對話", "effect": "遊戲通常可以執行，但 NPC 對話、角色互動、任務台詞或多話系統可能缺失、重複或錯亂。"}
    if any(k in text_all for k in ["asset", "texture", "mesh", "map", "world"]) or degree == "視覺 / 地圖異常":
        return {"impact_area": "地圖 / 材質 / 模型", "effect": "遊戲通常可以執行，但地圖、建築、角色裝備或物件可能破圖、紫色材質、模型缺失或碰撞異常。"}
    if "load order" in text_all or degree == "輕微覆蓋":
        return {"impact_area": "模組排序 / 覆蓋", "effect": "通常不會直接造成閃退，但可能導致設定被覆蓋、功能優先順序錯誤或模組效果不明顯。"}
    if degree == "無明顯衝突":
        return {"impact_area": "無", "effect": "目前資料庫中此組合被回報為可正常共存。"}
    return {"impact_area": "未知區域", "effect": "目前資料不足，建議使用 OpenAI 深度分析或補充錯誤描述。"}


def find_conflict(game, module_a, module_b):
    a, b = normalize_pair(module_a, module_b)
    return ConflictRule.query.filter_by(game=game, module_a=a, module_b=b).first()


def find_safe(game, module_a, module_b):
    a, b = normalize_pair(module_a, module_b)
    return SafeCombination.query.filter_by(game=game, module_a=a, module_b=b).first()


def observe_pair(game, module_a, module_b):
    """Record that a pair appeared in a user list. After repeated observations, auto-promote as candidate safe if no conflict exists."""
    a, b = normalize_pair(module_a, module_b)
    if ConflictRule.query.filter_by(game=game, module_a=a, module_b=b).first():
        return
    if SafeCombination.query.filter_by(game=game, module_a=a, module_b=b).first():
        return

    obs = ObservedPair.query.filter_by(game=game, module_a=a, module_b=b).first()
    if obs:
        obs.observation_count += 1
        obs.last_seen = datetime.utcnow()
    else:
        obs = ObservedPair(game=game, module_a=a, module_b=b, observation_count=1)
        db.session.add(obs)
        db.session.flush()

    if obs.observation_count >= AUTO_SAFE_THRESHOLD:
        db.session.add(SafeCombination(
            game=game,
            module_a=a,
            module_b=b,
            report_count=obs.observation_count,
            confidence_score=calculate_confidence(obs.observation_count),
            source="auto_observed"
        ))


def result_from_conflict(module_a, module_b, conflict):
    degree = normalize_conflict_degree(conflict.risk, conflict.conflict_type)
    impact = impact_description(conflict.conflict_type, degree)
    return {
        "module_a": module_a,
        "module_b": module_b,
        "status": "conflict",
        "conflict_type": conflict.conflict_type,
        "conflict_degree": degree,
        "impact_area": impact["impact_area"],
        "effect": impact["effect"],
        "confidence_score": conflict.confidence_score,
        "report_count": conflict.report_count,
        "source": conflict.source
    }


def result_from_safe(module_a, module_b, safe):
    impact = impact_description("None", "無明顯衝突")
    return {
        "module_a": module_a,
        "module_b": module_b,
        "status": "safe",
        "conflict_type": "None",
        "conflict_degree": "無明顯衝突",
        "impact_area": impact["impact_area"],
        "effect": impact["effect"],
        "confidence_score": safe.confidence_score,
        "report_count": safe.report_count,
        "source": safe.source
    }


def result_from_unknown(module_a, module_b):
    return {
        "module_a": module_a,
        "module_b": module_b,
        "status": "unknown",
        "conflict_type": "Unknown",
        "conflict_degree": "未知衝突",
        "impact_area": "未知",
        "effect": "資料庫尚未累積此組合的足夠資訊。可使用 OpenAI 深度分析或回報實際使用結果。",
        "confidence_score": 0,
        "report_count": 0,
        "source": "none"
    }


def sort_results(results):
    status_priority = {"conflict": 0, "unknown": 1, "safe": 2}
    degree_priority = {"啟動阻斷": 0, "高度崩潰風險": 1, "中度功能異常": 2, "視覺 / 地圖異常": 3, "輕微覆蓋": 4, "未知衝突": 5, "無明顯衝突": 6}
    return sorted(results, key=lambda x: (status_priority.get(x["status"], 9), degree_priority.get(x["conflict_degree"], 9), -float(x["confidence_score"])))


def summarize_results(results):
    summary = {"conflict": 0, "safe": 0, "unknown": 0, "degree_count": {}}
    for item in results:
        summary[item["status"]] += 1
        degree = item["conflict_degree"]
        summary["degree_count"][degree] = summary["degree_count"].get(degree, 0) + 1
    return summary


def clean_json_text(raw):
    text_value = raw.strip()
    if text_value.startswith("```json"):
        text_value = text_value.replace("```json", "", 1).strip()
    if text_value.startswith("```"):
        text_value = text_value.replace("```", "", 1).strip()
    if text_value.endswith("```"):
        text_value = text_value[:-3].strip()
    return text_value

# =========================
# OpenAI
# =========================

def save_ai_conflict_result(game, ai_data):
    conflicts = ai_data.get("likely_conflicts", [])
    for item in conflicts:
        module_a = item.get("module_a", "").strip()
        module_b = item.get("module_b", "").strip()
        if not module_a or not module_b:
            continue

        a, b = normalize_pair(module_a, module_b)
        conflict_type = item.get("conflict_type", "AI Predicted Conflict")
        degree = normalize_conflict_degree(item.get("conflict_degree") or item.get("risk"), conflict_type)

        rule = ConflictRule.query.filter_by(game=game, module_a=a, module_b=b).first()
        if rule:
            rule.report_count += 1
            rule.confidence_score = calculate_confidence(rule.report_count)
            rule.conflict_type = conflict_type
            rule.risk = degree
            rule.source = "openai"
        else:
            db.session.add(ConflictRule(
                game=game,
                module_a=a,
                module_b=b,
                conflict_type=conflict_type,
                risk=degree,
                report_count=1,
                confidence_score=float(item.get("confidence_score", 0.55)),
                source="openai"
            ))
    db.session.commit()


def call_openai_analysis(game, mods, crash_log=""):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=api_key)
    game_label = GAME_PROFILES.get(game, GAME_PROFILES["generic"])["label"]

    prompt = f"""
你是一位遊戲模組相容性分析系統。

目前分析的遊戲：{game_label}

請根據模組列表與錯誤描述進行相容性預測。玩家可能只提供模組名稱，不一定提供副檔名。
如果沒有 crash log，也要根據模組名稱、常見模組生態、功能重疊來做保守預測；但不可捏造不存在的 log。

不同遊戲注意：
- Skyrim / Fallout：load order、script、skeleton、dialogue、asset、dependency。
- Stardew Valley：SMAPI、Content Patcher、地圖覆蓋、事件腳本、NPC 對話。
- Minecraft：loader 不相容、library 缺失、mixin crash、版本不符、渲染模組衝突。
- Cyberpunk 2077：ArchiveXL、TweakXL、REDscript、CET、材質或腳本覆蓋。

可用衝突類型：
- Skeleton Conflict
- Script Override
- Dialogue Conflict
- Asset Conflict
- Load Order Conflict
- Dependency Missing
- Loader / Version Conflict
- Map / World Conflict
- Unknown Conflict

可用衝突程度：
- 啟動阻斷
- 高度崩潰風險
- 中度功能異常
- 視覺 / 地圖異常
- 輕微覆蓋
- 未知衝突

模組列表：
{json.dumps(mods, ensure_ascii=False)}

錯誤描述 / Crash Log：
{crash_log}

只輸出 JSON，不要 Markdown。格式：
{{
  "summary": "整體分析摘要",
  "overall_conflict_degree": "中度功能異常",
  "likely_conflicts": [
    {{
      "module_a": "xxx",
      "module_b": "yyy",
      "conflict_type": "Dialogue Conflict",
      "conflict_degree": "中度功能異常",
      "impact_area": "角色 / 對話",
      "player_effect": "遊戲可執行，但角色對話或多話系統可能出現錯亂。",
      "confidence_score": 0.75,
      "reason": "判斷原因",
      "suggestion": "建議處理方式"
    }}
  ]
}}
"""

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "你是專業的遊戲模組衝突分析助理，只輸出 JSON。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2,
        response_format={"type": "json_object"}
    )
    content = clean_json_text(response.choices[0].message.content)
    try:
        return json.loads(content)
    except Exception:
        return {"summary": "OpenAI 回傳格式無法解析，但仍保留原始內容。", "overall_conflict_degree": "未知衝突", "likely_conflicts": [], "raw_output": content}

# =========================
# Page Routes
# =========================

@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/pricing")
def pricing():
    return render_template("pricing.html")


@app.route("/analyzer")
@login_required
def analyzer_page():
    return render_template("analyzer.html")


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    analyze_used = today_usage_count(user.id, "analyze")
    ai_used = today_usage_count(user.id, "ai_analyze")
    plan = get_plan_for_user(user)
    recent = RawReport.query.filter_by(user_id=user.id).order_by(RawReport.created_at.desc()).limit(10).all()
    return render_template("dashboard.html", analyze_used=analyze_used, ai_used=ai_used, plan=plan, recent=recent)


@app.route("/database")
def database_page():
    game = request.args.get("game", "skyrim")
    if game not in GAME_PROFILES:
        game = "skyrim"

    conflicts = ConflictRule.query.filter_by(game=game).order_by(ConflictRule.report_count.desc()).limit(50).all()
    safes = SafeCombination.query.filter_by(game=game).order_by(SafeCombination.report_count.desc()).limit(50).all()
    return render_template("database.html", selected_game=game, conflicts=conflicts, safes=safes)

# =========================
# Auth Routes
# =========================

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        plan = request.form.get("plan", "free")
        if plan not in PLAN_LIMITS:
            plan = "free"

        if not username or not email or not password:
            flash("請完整填寫註冊資料。", "danger")
            return redirect(url_for("register"))

        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("帳號或 Email 已被使用。", "danger")
            return redirect(url_for("register"))

        user = User(username=username, email=email, password_hash=generate_password_hash(password), plan=plan)
        db.session.add(user)
        db.session.commit()
        session["user_id"] = user.id
        flash("註冊成功。", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        account = request.form.get("account", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter((User.username == account) | (User.email == account.lower())).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("帳號或密碼錯誤。", "danger")
            return redirect(url_for("login"))
        session["user_id"] = user.id
        flash("登入成功。", "success")
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("已登出。", "success")
    return redirect(url_for("landing"))


@app.route("/change_plan", methods=["POST"])
@login_required
def change_plan():
    plan = request.form.get("plan", "free")
    if plan not in PLAN_LIMITS:
        flash("方案不存在。", "danger")
        return redirect(url_for("pricing"))
    user = current_user()
    user.plan = plan
    db.session.commit()
    flash(f"已切換至 {PLAN_LIMITS[plan]['zh_name']}。", "success")
    return redirect(url_for("dashboard"))

# =========================
# API Routes
# =========================

@app.route("/api/analyze", methods=["POST"])
@login_required
def api_analyze():
    user = current_user()
    plan = get_plan_for_user(user)
    allowed, used, limit = check_usage_allowed(user, "analyze")
    if not allowed:
        return jsonify({"error": f"今日資料庫分析次數已用完：{used}/{limit}。請升級方案或明天再試。"}), 403

    game = get_game_from_request()
    file = request.files.get("file")
    manual_text = request.form.get("mod_text", "")
    content = ""
    if file:
        content += file.read().decode("utf-8", errors="ignore")
    if manual_text.strip():
        content += "\n" + manual_text
    if not content.strip():
        return jsonify({"error": "請上傳檔案或直接輸入模組名稱。"}), 400

    mods = extract_mods(content, game)
    if not mods:
        return jsonify({"error": "沒有辨識到模組名稱。請確認是一行一個模組。"}), 400
    if len(mods) > plan["max_mods"]:
        return jsonify({"error": f"{plan['zh_name']} 最多支援 {plan['max_mods']} 個模組。你目前輸入 {len(mods)} 個。"}), 403

    results = []
    for module_a, module_b in combinations(mods, 2):
        conflict = find_conflict(game, module_a, module_b)
        safe = find_safe(game, module_a, module_b)
        if conflict:
            results.append(result_from_conflict(module_a, module_b, conflict))
        elif safe:
            results.append(result_from_safe(module_a, module_b, safe))
        else:
            observe_pair(game, module_a, module_b)
            results.append(result_from_unknown(module_a, module_b))

    results = sort_results(results)
    summary = summarize_results(results)

    db.session.add(RawReport(game=game, user_id=user.id, report_type="analyze", content=content))
    record_usage(user, "analyze")
    db.session.commit()

    display_results = results[:plan["max_display_pairs"]]
    return jsonify({
        "game": game,
        "game_label": GAME_PROFILES[game]["label"],
        "plan": user.plan,
        "plan_name": plan["zh_name"],
        "mods_detected": mods,
        "total_mods": len(mods),
        "total_pairs_checked": len(results),
        "displayed_pairs": len(display_results),
        "usage": {"used": used + 1, "limit": limit},
        "summary": summary,
        "results": display_results
    })


@app.route("/api/ai_analyze", methods=["POST"])
@login_required
def api_ai_analyze():
    user = current_user()
    plan = get_plan_for_user(user)
    if not plan["allow_ai"]:
        return jsonify({"error": "你的方案不包含 OpenAI 深度分析。請升級 Basic、Pro 或 Creator。"}), 403
    allowed, used, limit = check_usage_allowed(user, "ai_analyze")
    if not allowed:
        return jsonify({"error": f"今日 OpenAI 分析次數已用完：{used}/{limit}。"}), 403
    if not os.environ.get("OPENAI_API_KEY"):
        return jsonify({"error": "伺服器尚未設定 OPENAI_API_KEY。"}), 500

    game = get_game_from_request()
    file = request.files.get("file")
    manual_text = request.form.get("mod_text", "")
    crash_log = request.form.get("crash_log", "")
    content = ""
    if file:
        content += file.read().decode("utf-8", errors="ignore")
    if manual_text.strip():
        content += "\n" + manual_text
    if not content.strip():
        return jsonify({"error": "請上傳檔案或直接輸入模組名稱。"}), 400

    mods = extract_mods(content, game)
    if not mods:
        return jsonify({"error": "沒有辨識到模組名稱。請確認是一行一個模組。"}), 400
    if len(mods) > plan["max_mods"]:
        return jsonify({"error": f"{plan['zh_name']} 最多支援 {plan['max_mods']} 個模組。你目前輸入 {len(mods)} 個。"}), 403

    ai_result = call_openai_analysis(game, mods, crash_log)
    db.session.add(RawReport(
        game=game,
        user_id=user.id,
        report_type="ai_analyze",
        content=content + "\n\n錯誤描述 / Crash Log:\n" + crash_log,
        ai_result=json.dumps(ai_result, ensure_ascii=False)
    ))
    save_ai_conflict_result(game, ai_result)
    record_usage(user, "ai_analyze")

    return jsonify({
        "game": game,
        "game_label": GAME_PROFILES[game]["label"],
        "plan": user.plan,
        "plan_name": plan["zh_name"],
        "mods_detected": mods,
        "usage": {"used": used + 1, "limit": limit},
        "ai_result": ai_result
    })


@app.route("/api/report_conflict", methods=["POST"])
@login_required
def api_report_conflict():
    data = request.json or {}
    game = get_game_from_json(data)
    module_a = data.get("module_a", "").strip()
    module_b = data.get("module_b", "").strip()
    conflict_type = data.get("conflict_type", "User Reported Conflict")
    degree = normalize_conflict_degree(data.get("conflict_degree", ""), conflict_type)
    if not module_a or not module_b:
        return jsonify({"error": "module_a 與 module_b 必填"}), 400

    a, b = normalize_pair(module_a, module_b)
    rule = ConflictRule.query.filter_by(game=game, module_a=a, module_b=b).first()
    if rule:
        rule.report_count += 1
        rule.confidence_score = calculate_confidence(rule.report_count)
        rule.conflict_type = conflict_type
        rule.risk = degree
        rule.source = "user"
    else:
        db.session.add(ConflictRule(game=game, module_a=a, module_b=b, conflict_type=conflict_type, risk=degree, report_count=1, confidence_score=calculate_confidence(1), source="user"))
    db.session.commit()
    return jsonify({"status": "success", "message": "衝突回報已寫入資料庫。"})


@app.route("/api/report_safe", methods=["POST"])
@login_required
def api_report_safe():
    data = request.json or {}
    game = get_game_from_json(data)
    module_a = data.get("module_a", "").strip()
    module_b = data.get("module_b", "").strip()
    if not module_a or not module_b:
        return jsonify({"error": "module_a 與 module_b 必填"}), 400

    a, b = normalize_pair(module_a, module_b)
    safe = SafeCombination.query.filter_by(game=game, module_a=a, module_b=b).first()
    if safe:
        safe.report_count += 1
        safe.confidence_score = calculate_confidence(safe.report_count)
        safe.source = "user"
    else:
        db.session.add(SafeCombination(game=game, module_a=a, module_b=b, report_count=1, confidence_score=calculate_confidence(1), source="user"))
    db.session.commit()
    return jsonify({"status": "success", "message": "安全組合已寫入資料庫。"})


@app.route("/api/seed_demo", methods=["POST"])
@login_required
def api_seed_demo():
    demo_safe = [
        ("skyrim", "SkyUI", "XPMSSE"),
        ("skyrim", "Unofficial Skyrim Special Edition Patch", "SkyUI"),
        ("stardew", "SMAPI", "Content Patcher"),
        ("stardew", "Content Patcher", "Stardew Valley Expanded"),
        ("minecraft", "Fabric API", "Sodium"),
        ("minecraft", "Fabric API", "Lithium"),
        ("cyberpunk", "Cyber Engine Tweaks", "ArchiveXL"),
    ]
    demo_conflicts = [
        ("skyrim", "XPMSSE", "CombatAnimation", "Skeleton Conflict", "高度崩潰風險"),
        ("skyrim", "Serana Dialogue Add-On", "Relationship Dialogue Overhaul", "Dialogue Conflict", "中度功能異常"),
        ("stardew", "Stardew Valley Expanded", "Another Farm Map", "Map / World Conflict", "視覺 / 地圖異常"),
        ("minecraft", "Sodium", "OptiFine", "Loader / Version Conflict", "啟動阻斷"),
        ("cyberpunk", "ArchiveXL", "Old Archive Mod", "Asset Conflict", "視覺 / 地圖異常"),
    ]

    for game, a_raw, b_raw in demo_safe:
        a, b = normalize_pair(a_raw, b_raw)
        if not SafeCombination.query.filter_by(game=game, module_a=a, module_b=b).first():
            db.session.add(SafeCombination(game=game, module_a=a, module_b=b, report_count=8, confidence_score=0.9, source="demo"))

    for game, a_raw, b_raw, ctype, degree in demo_conflicts:
        a, b = normalize_pair(a_raw, b_raw)
        if not ConflictRule.query.filter_by(game=game, module_a=a, module_b=b).first():
            db.session.add(ConflictRule(game=game, module_a=a, module_b=b, conflict_type=ctype, risk=degree, report_count=12, confidence_score=0.95, source="demo"))

    db.session.commit()
    return jsonify({"status": "success", "message": "示範資料已建立。"})


@app.route("/api/stats")
def api_stats():
    game = request.args.get("game", "skyrim").lower().strip()
    if game not in GAME_PROFILES:
        game = "skyrim"

    conflicts = ConflictRule.query.filter_by(game=game).order_by(ConflictRule.report_count.desc()).limit(50).all()
    safes = SafeCombination.query.filter_by(game=game).order_by(SafeCombination.report_count.desc()).limit(50).all()
    return jsonify({
        "game": game,
        "game_label": GAME_PROFILES[game]["label"],
        "conflict_count": len(conflicts),
        "safe_count": len(safes)
    })


with app.app_context():
    ensure_schema()

if __name__ == "__main__":
    app.run(debug=True)
