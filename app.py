from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from sqlalchemy import inspect, text, or_, and_
from sqlalchemy.exc import OperationalError, InterfaceError
from werkzeug.security import generate_password_hash, check_password_hash
from itertools import combinations
from datetime import datetime, date
from openai import OpenAI
import os
import re
import json
import random


# ============================================================
# App Config
# ============================================================

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-this")

database_url = os.environ.get("DATABASE_URL")

if database_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url.replace("postgres://", "postgresql://")
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///modcrash.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Render PostgreSQL / Free Plan connection stability
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
    "pool_timeout": 30,
    "pool_size": 5,
    "max_overflow": 2,
}

db = SQLAlchemy(app, session_options={"expire_on_commit": False})

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
AUTO_SAFE_THRESHOLD = int(os.environ.get("AUTO_SAFE_THRESHOLD", "5"))
UNKNOWN_OBSERVE_LIMIT = int(os.environ.get("UNKNOWN_OBSERVE_LIMIT", "100"))

# API response caps: prevent /api/analyze from returning multi-MB JSON.
MAX_CONFLICT_RESULTS = int(os.environ.get("MAX_CONFLICT_RESULTS", "80"))
MAX_SAFE_RESULTS = int(os.environ.get("MAX_SAFE_RESULTS", "80"))
MAX_UNKNOWN_RESULTS = int(os.environ.get("MAX_UNKNOWN_RESULTS", "30"))


# ============================================================
# Game Profiles
# ============================================================

GAME_PROFILES = {
    "skyrim": {
        "label": "Skyrim / Skyrim Special Edition",
        "icon": "🏔️",
        "extensions": [".esp", ".esm", ".esl"],
        "examples": ["SkyUI", "XPMSSE", "Serana Dialogue Add-On"],
        "theme": "Nordic Mountains"
    },
    "fallout4": {
        "label": "Fallout 4",
        "icon": "⚙️",
        "extensions": [".esp", ".esm", ".esl"],
        "examples": ["LooksMenu", "Sim Settlements 2", "Full Dialogue Interface"],
        "theme": "Wasteland Engineering"
    },
    "stardew": {
        "label": "Stardew Valley",
        "icon": "🌾",
        "extensions": [".dll", ".json", ".zip"],
        "examples": ["SMAPI", "Content Patcher", "Stardew Valley Expanded"],
        "theme": "Valley Community"
    },
    "minecraft": {
        "label": "Minecraft",
        "icon": "🧊",
        "extensions": [".jar", ".zip"],
        "examples": ["Fabric API", "Sodium", "Create"],
        "theme": "Block Federation"
    },
    "cyberpunk": {
        "label": "Cyberpunk 2077",
        "icon": "🌆",
        "extensions": [".archive", ".xl", ".reds", ".lua"],
        "examples": ["Cyber Engine Tweaks", "ArchiveXL", "TweakXL"],
        "theme": "Neon Protocol"
    },
    "generic": {
        "label": "其他遊戲 / 通用",
        "icon": "🌐",
        "extensions": [],
        "examples": ["Mod A", "Mod B", "Texture Pack"],
        "theme": "Open Alliance"
    }
}


# ============================================================
# Plan Settings
# ============================================================

PLAN_LIMITS = {
    "free": {
        "name": "Free",
        "price": "$0",
        "daily_analyze_limit": 3,
        "daily_ai_limit": 0,
        "max_mods": 20,
        "max_display_pairs": 80,
        "allow_ai": False
    },
    "basic": {
        "name": "Basic",
        "price": "$5 / month",
        "daily_analyze_limit": 20,
        "daily_ai_limit": 5,
        "max_mods": 80,
        "max_display_pairs": 800,
        "allow_ai": True
    },
    "pro": {
        "name": "Pro",
        "price": "$12 / month",
        "daily_analyze_limit": 100,
        "daily_ai_limit": 25,
        "max_mods": 150,
        "max_display_pairs": 3000,
        "allow_ai": True
    },
    "creator": {
        "name": "Creator",
        "price": "$29 / month",
        "daily_analyze_limit": 300,
        "daily_ai_limit": 100,
        "max_mods": 300,
        "max_display_pairs": 8000,
        "allow_ai": True
    }
}


# ============================================================
# Models
# ============================================================

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    plan = db.Column(db.String(50), default="free")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class UsageLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    action_type = db.Column(db.String(50), nullable=False)  # analyze / ai
    used_at = db.Column(db.DateTime, default=datetime.utcnow)


class ConflictRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(80), default="skyrim")
    module_a = db.Column(db.String(255), nullable=False)
    module_b = db.Column(db.String(255), nullable=False)
    conflict_type = db.Column(db.String(120), default="Unknown Conflict")
    risk = db.Column(db.String(80), default="中度功能異常")  # stored as conflict_degree
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


class UnknownObservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(80), default="skyrim")
    module_a = db.Column(db.String(255), nullable=False)
    module_b = db.Column(db.String(255), nullable=False)
    observe_count = db.Column(db.Integer, default=1)
    promoted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RawReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(80), default="skyrim")
    user_id = db.Column(db.Integer, nullable=True)
    report_type = db.Column(db.String(50), nullable=False)
    content = db.Column(db.Text, nullable=False)
    ai_result = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ============================================================
# Stable DB helpers
# ============================================================

@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (OperationalError, InterfaceError):
        db.session.rollback()
        db.engine.dispose()
        try:
            return db.session.get(User, int(user_id))
        except Exception:
            db.session.rollback()
            return None


@app.errorhandler(OperationalError)
def handle_operational_error(error):
    db.session.rollback()
    db.engine.dispose()
    return jsonify({
        "error": "資料庫連線暫時中斷，請重新整理頁面後再試一次。",
        "detail": "Database connection was closed by the server."
    }), 500


def ensure_schema():
    db.create_all()

    inspector = inspect(db.engine)

    # Existing deployed DB may miss columns after version upgrade.
    migrations = {
        "user": {
            "plan": "VARCHAR(50) DEFAULT 'free'",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        },
        "conflict_rule": {
            "game": "VARCHAR(80) DEFAULT 'skyrim'",
            "source": "VARCHAR(50) DEFAULT 'user'",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        },
        "safe_combination": {
            "game": "VARCHAR(80) DEFAULT 'skyrim'",
            "source": "VARCHAR(50) DEFAULT 'user'",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        },
        "raw_report": {
            "game": "VARCHAR(80) DEFAULT 'skyrim'",
            "user_id": "INTEGER",
            "ai_result": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        }
    }

    with db.engine.begin() as conn:
        for table_name, columns in migrations.items():
            if not inspector.has_table(table_name):
                continue
            existing_cols = [col["name"] for col in inspector.get_columns(table_name)]
            for col_name, col_type in columns.items():
                if col_name not in existing_cols:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"))


# ============================================================
# Parsing and Normalization
# ============================================================

def clean_mod_line(line):
    line = line.strip()
    if not line:
        return ""

    line = line.replace("\ufeff", "")
    line = line.replace("*", "").strip()

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

    line = re.sub(r"\s+\(enabled\)$", "", line, flags=re.IGNORECASE)
    line = re.sub(r"\s+\(disabled\)$", "", line, flags=re.IGNORECASE)
    line = re.sub(r"\s+\[enabled\]$", "", line, flags=re.IGNORECASE)
    line = re.sub(r"\s+\[disabled\]$", "", line, flags=re.IGNORECASE)

    return line.strip()


def is_noise_line(line):
    if not line:
        return True

    lower = line.lower().strip()

    noise_keywords = [
        "plugins", "plugin list", "load order", "mod list",
        "active mods", "inactive mods", "enabled mods", "disabled mods",
        "crash log", "stack trace", "error", "warning", "----", "===="
    ]

    if lower in noise_keywords:
        return True

    if len(line) > 120:
        return True

    return False


def normalize_mod_name(name):
    name = clean_mod_line(name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip("\"'")
    return name


def normalize_pair(a, b):
    a = normalize_mod_name(a)
    b = normalize_mod_name(b)
    return tuple(sorted([a, b], key=lambda x: x.lower()))


def extract_mods(text, game="generic"):
    mods = []
    profile = GAME_PROFILES.get(game, GAME_PROFILES["generic"])
    extensions = profile.get("extensions", [])

    for raw_line in text.splitlines():
        line = normalize_mod_name(raw_line)

        if is_noise_line(line):
            continue

        if extensions:
            lower = line.lower()
            if any(lower.endswith(ext.lower()) for ext in extensions):
                mods.append(line)
                continue

        if 2 <= len(line) <= 80:
            sentence_marks = ["。", "，", "；", "："]
            if not any(mark in line for mark in sentence_marks):
                mods.append(line)

    seen = set()
    clean_mods = []

    for mod in mods:
        key = mod.lower()
        if key not in seen:
            seen.add(key)
            clean_mods.append(mod)

    return clean_mods


# ============================================================
# Conflict Logic
# ============================================================

def calculate_confidence(count):
    return round(min(0.35 + count * 0.08, 0.98), 2)


def default_degree_by_type(conflict_type):
    conflict_type = (conflict_type or "").lower()

    if "dependency" in conflict_type or "missing" in conflict_type:
        return "啟動阻斷"
    if "loader" in conflict_type or "version" in conflict_type:
        return "啟動阻斷"
    if "skeleton" in conflict_type:
        return "高度崩潰風險"
    if "script" in conflict_type:
        return "中度功能異常"
    if "dialogue" in conflict_type or "dialog" in conflict_type:
        return "中度功能異常"
    if "asset" in conflict_type or "texture" in conflict_type or "mesh" in conflict_type:
        return "視覺 / 地圖異常"
    if "map" in conflict_type or "world" in conflict_type:
        return "視覺 / 地圖異常"
    if "load order" in conflict_type:
        return "輕微覆蓋"

    return "中度功能異常"


def normalize_conflict_degree(value, conflict_type="Unknown Conflict"):
    if not value:
        return default_degree_by_type(conflict_type)

    value = value.strip()

    legacy_map = {
        "High": "高度崩潰風險",
        "Medium": "中度功能異常",
        "Low": "輕微覆蓋",
        "Unknown": "未知衝突"
    }

    valid = [
        "啟動阻斷", "高度崩潰風險", "中度功能異常",
        "視覺 / 地圖異常", "輕微覆蓋", "未知衝突", "無明顯衝突"
    ]

    if value in legacy_map:
        return legacy_map[value]
    if value in valid:
        return value

    return default_degree_by_type(conflict_type)


def impact_description(conflict_type, conflict_degree):
    text_all = f"{conflict_type} {conflict_degree}".lower()

    if "dependency" in text_all or "missing" in text_all or conflict_degree == "啟動阻斷":
        return {
            "impact_area": "啟動 / 讀檔",
            "effect": "可能缺少前置模組或版本不相容，遊戲可能無法啟動、讀檔閃退，或在主選單前崩潰。"
        }
    if "loader" in text_all or "version" in text_all:
        return {
            "impact_area": "模組載入器 / 版本",
            "effect": "可能因遊戲版本、模組載入器或必要函式庫不同，造成啟動失敗或進入世界時崩潰。"
        }
    if "skeleton" in text_all:
        return {
            "impact_area": "角色骨架 / 動作",
            "effect": "遊戲可能可啟動，但角色可能 T-Pose、動作異常、戰鬥動畫錯誤，嚴重時會崩潰。"
        }
    if "script" in text_all:
        return {
            "impact_area": "任務 / 腳本",
            "effect": "遊戲通常可執行，但任務可能卡住、功能不觸發、事件失效，長時間遊玩後可能出現存檔污染。"
        }
    if "dialogue" in text_all or "dialog" in text_all:
        return {
            "impact_area": "角色 / 對話",
            "effect": "遊戲通常可執行，但 NPC 對話、角色互動、任務台詞或多話系統可能缺失、重複或錯亂。"
        }
    if (
        "asset" in text_all or "texture" in text_all or "mesh" in text_all
        or "map" in text_all or "world" in text_all
        or conflict_degree == "視覺 / 地圖異常"
    ):
        return {
            "impact_area": "地圖 / 材質 / 模型",
            "effect": "遊戲通常可執行，但地圖、建築、角色裝備或物件可能破圖、紫色材質、模型缺失或碰撞異常。"
        }
    if "load order" in text_all or conflict_degree == "輕微覆蓋":
        return {
            "impact_area": "模組排序 / 覆蓋",
            "effect": "通常不會直接閃退，但可能導致部分設定被覆蓋、功能優先順序錯誤或模組效果不明顯。"
        }
    if conflict_degree == "無明顯衝突":
        return {
            "impact_area": "無",
            "effect": "目前資料庫中此組合被回報為可正常共存。"
        }

    return {
        "impact_area": "未知區域",
        "effect": "目前資料不足，僅能判斷此組合可能存在相容性問題，建議使用 AI 深度分析或提供錯誤描述。"
    }


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
        "effect": "資料庫尚未累積此組合的足夠資訊。可使用 AI 深度分析，或等待更多玩家回報。",
        "confidence_score": 0,
        "report_count": 0,
        "source": "none"
    }


def sort_results(results):
    status_priority = {"conflict": 0, "unknown": 1, "safe": 2}
    degree_priority = {
        "啟動阻斷": 0,
        "高度崩潰風險": 1,
        "中度功能異常": 2,
        "視覺 / 地圖異常": 3,
        "輕微覆蓋": 4,
        "未知衝突": 5,
        "無明顯衝突": 6
    }

    return sorted(
        results,
        key=lambda x: (
            status_priority.get(x["status"], 9),
            degree_priority.get(x["conflict_degree"], 9),
            -float(x["confidence_score"])
        )
    )


def summarize_results(results):
    summary = {"conflict": 0, "safe": 0, "unknown": 0, "degree_count": {}}

    for item in results:
        summary[item["status"]] += 1
        degree = item["conflict_degree"]
        summary["degree_count"][degree] = summary["degree_count"].get(degree, 0) + 1

    return summary


def build_pair_key(game, a, b):
    na, nb = normalize_pair(a, b)
    return (game, na.lower(), nb.lower())


def batch_fetch_rules(game, pairs):
    normalized = []
    for a, b in pairs:
        na, nb = normalize_pair(a, b)
        normalized.append((na, nb))

    # Query all rows for this game once; easier and fast enough for current plan scale.
    conflicts = ConflictRule.query.filter_by(game=game).all()
    safes = SafeCombination.query.filter_by(game=game).all()

    conflict_map = {
        (c.module_a.lower(), c.module_b.lower()): c for c in conflicts
    }
    safe_map = {
        (s.module_a.lower(), s.module_b.lower()): s for s in safes
    }

    return conflict_map, safe_map


def observe_unknown_pairs(game, unknown_pairs):
    if not unknown_pairs:
        return

    # If the list is large, do not always take the first N pairs.
    # Random sampling lets repeated analyses gradually observe different unknown pairs.
    if len(unknown_pairs) > UNKNOWN_OBSERVE_LIMIT:
        sample = random.sample(unknown_pairs, UNKNOWN_OBSERVE_LIMIT)
    else:
        sample = unknown_pairs

    for a_raw, b_raw in sample:
        a, b = normalize_pair(a_raw, b_raw)

        obs = UnknownObservation.query.filter_by(
            game=game,
            module_a=a,
            module_b=b
        ).first()

        if obs:
            obs.observe_count += 1
        else:
            obs = UnknownObservation(
                game=game,
                module_a=a,
                module_b=b,
                observe_count=1,
                promoted=False
            )
            db.session.add(obs)

        # Auto-promote repeated unknown observations into SafeCombination.
        # Important:
        # Some older versions set obs.promoted=True without creating SafeCombination.
        # Therefore, once the threshold is reached, always verify whether the safe row exists.
        if obs.observe_count >= AUTO_SAFE_THRESHOLD:
            existing_safe = SafeCombination.query.filter_by(
                game=game,
                module_a=a,
                module_b=b
            ).first()
            existing_conflict = ConflictRule.query.filter_by(
                game=game,
                module_a=a,
                module_b=b
            ).first()

            if not existing_safe and not existing_conflict:
                db.session.add(SafeCombination(
                    game=game,
                    module_a=a,
                    module_b=b,
                    report_count=obs.observe_count,
                    confidence_score=calculate_confidence(obs.observe_count),
                    source="auto_candidate"
                ))

            obs.promoted = True


# ============================================================
# Usage / Plans
# ============================================================

def today_usage_count(user_id, action_type):
    start = datetime.combine(date.today(), datetime.min.time())
    return UsageLog.query.filter(
        UsageLog.user_id == user_id,
        UsageLog.action_type == action_type,
        UsageLog.used_at >= start
    ).count()


def check_usage_or_error(action_type):
    if not current_user.is_authenticated:
        return "請先登入後再使用分析功能。"

    plan_key = current_user.plan if current_user.plan in PLAN_LIMITS else "free"
    limits = PLAN_LIMITS[plan_key]

    if action_type == "analyze":
        used = today_usage_count(current_user.id, "analyze")
        if used >= limits["daily_analyze_limit"]:
            return f"{limits['name']} 今日快速分析次數已用完。"

    if action_type == "ai":
        if not limits["allow_ai"]:
            return "AI 深度分析屬於 Basic / Pro / Creator 功能。"
        used = today_usage_count(current_user.id, "ai")
        if used >= limits["daily_ai_limit"]:
            return f"{limits['name']} 今日 AI 分析次數已用完。"

    return None


def record_usage(action_type):
    db.session.add(UsageLog(user_id=current_user.id, action_type=action_type))
    db.session.commit()


# ============================================================
# OpenAI
# ============================================================

def clean_json_text(text_value):
    text_value = text_value.strip()

    if text_value.startswith("```json"):
        text_value = text_value.replace("```json", "", 1).strip()
    if text_value.startswith("```"):
        text_value = text_value.replace("```", "", 1).strip()
    if text_value.endswith("```"):
        text_value = text_value[:-3].strip()

    return text_value


def extract_ai_conflict_items(ai_data):
    """
    Accept several possible OpenAI JSON shapes.
    Main expected key: likely_conflicts.
    Fallback keys are included because model output may vary.
    """
    if not isinstance(ai_data, dict):
        return []

    for key in ["likely_conflicts", "conflicts", "predicted_conflicts", "detected_conflicts"]:
        value = ai_data.get(key)
        if isinstance(value, list):
            return value

    return []


def save_ai_conflict_result(game, ai_data):
    """
    Save OpenAI-predicted conflict pairs into ConflictRule.
    This function intentionally saves only conflict items.
    If OpenAI returns no likely_conflicts, only RawReport will be saved by /api/ai_analyze.
    """
    conflicts = extract_ai_conflict_items(ai_data)
    saved_count = 0
    skipped_count = 0
    saved_pairs = []

    for item in conflicts:
        if not isinstance(item, dict):
            skipped_count += 1
            continue

        module_a = str(item.get("module_a", "") or "").strip()
        module_b = str(item.get("module_b", "") or "").strip()

        # Fallback: allow {"modules": ["A", "B"]}.
        modules = item.get("modules")
        if (not module_a or not module_b) and isinstance(modules, list) and len(modules) >= 2:
            module_a = str(modules[0]).strip()
            module_b = str(modules[1]).strip()

        if not module_a or not module_b:
            skipped_count += 1
            continue

        a, b = normalize_pair(module_a, module_b)

        conflict_type = item.get("conflict_type", "AI Predicted Conflict")
        conflict_degree = (
            item.get("conflict_degree")
            or item.get("risk")
            or item.get("severity")
            or default_degree_by_type(conflict_type)
        )
        conflict_degree = normalize_conflict_degree(str(conflict_degree), conflict_type)

        try:
            confidence = float(item.get("confidence_score", 0.55))
        except Exception:
            confidence = 0.55

        rule = ConflictRule.query.filter_by(
            game=game,
            module_a=a,
            module_b=b
        ).first()

        if rule:
            rule.report_count += 1
            rule.confidence_score = max(rule.confidence_score or 0, calculate_confidence(rule.report_count), confidence)
            rule.conflict_type = conflict_type
            rule.risk = conflict_degree
            rule.source = "openai"
        else:
            rule = ConflictRule(
                game=game,
                module_a=a,
                module_b=b,
                conflict_type=conflict_type,
                risk=conflict_degree,
                report_count=1,
                confidence_score=confidence,
                source="openai"
            )
            db.session.add(rule)

        saved_count += 1
        saved_pairs.append({
            "module_a": a,
            "module_b": b,
            "conflict_type": conflict_type,
            "conflict_degree": conflict_degree
        })

    db.session.commit()

    return {
        "saved_count": saved_count,
        "skipped_count": skipped_count,
        "saved_pairs": saved_pairs
    }


def call_openai_analysis(game, mods, crash_log=""):
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=api_key)

    game_label = GAME_PROFILES.get(game, GAME_PROFILES["generic"])["label"]

    prompt = f"""
你是一位遊戲模組相容性分析系統。

目前分析的遊戲是：
{game_label}

請根據以下模組列表與錯誤描述，分析可能的模組衝突。

注意：
1. 玩家可能只提供模組名稱，不一定提供副檔名。
2. 請依照目前遊戲的模組生態判斷衝突。
3. 如果資訊不足，請明確說明需要更多資料，不要捏造不存在的 crash log。
4. 請只針對輸入模組之間的相容性做判斷。

遊戲判斷方向：
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

請輸出 JSON：
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
        return {
            "summary": "OpenAI 回傳格式無法解析，但仍保留原始內容。",
            "overall_conflict_degree": "未知衝突",
            "likely_conflicts": [],
            "raw_output": content
        }


# ============================================================
# Page Routes
# ============================================================

@app.route("/")
def landing():
    return render_template("landing.html", games=GAME_PROFILES)


@app.route("/analyzer")
@login_required
def analyzer():
    return render_template(
        "analyzer.html",
        games=GAME_PROFILES,
        plans=PLAN_LIMITS,
        user_plan=current_user.plan
    )


@app.route("/pricing")
def pricing():
    return render_template("pricing.html", plans=PLAN_LIMITS)


@app.route("/database")
def database_page():
    return render_template("database.html", games=GAME_PROFILES)


@app.route("/dashboard")
@login_required
def dashboard():
    analyze_used = today_usage_count(current_user.id, "analyze")
    ai_used = today_usage_count(current_user.id, "ai")
    plan = PLAN_LIMITS.get(current_user.plan, PLAN_LIMITS["free"])

    return render_template(
        "dashboard.html",
        plan=plan,
        analyze_used=analyze_used,
        ai_used=ai_used,
        plans=PLAN_LIMITS
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("請完整填寫註冊資料。")
            return redirect(url_for("register"))

        existing = User.query.filter(or_(User.username == username, User.email == email)).first()
        if existing:
            flash("使用者名稱或 Email 已被註冊。")
            return redirect(url_for("register"))

        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            plan="free"
        )
        db.session.add(user)
        db.session.commit()

        login_user(user)
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        account = request.form.get("account", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter(or_(User.username == account, User.email == account.lower())).first()

        if not user or not check_password_hash(user.password_hash, password):
            flash("帳號或密碼錯誤。")
            return redirect(url_for("login"))

        login_user(user)
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("landing"))


@app.route("/switch_plan/<plan_key>", methods=["POST"])
@login_required
def switch_plan(plan_key):
    if plan_key not in PLAN_LIMITS:
        flash("方案不存在。")
        return redirect(url_for("pricing"))

    current_user.plan = plan_key
    db.session.commit()
    flash(f"已切換到 {PLAN_LIMITS[plan_key]['name']}。")
    return redirect(url_for("dashboard"))


# ============================================================
# API Routes
# ============================================================

@app.route("/api/analyze", methods=["POST"])
@login_required
def api_analyze():
    """
    Compact database analysis.

    Important:
    Older versions returned every pair in JSON. If users upload 100+ mods, pair count becomes thousands:
        80 mods  = 3,160 pairs
        150 mods = 11,175 pairs
    Returning every unknown pair can create multi-MB responses and make the browser feel stuck.

    This version still checks every pair for summary counts, but only returns:
    - limited conflict records
    - limited safe records
    - limited unknown sample records
    """
    error = check_usage_or_error("analyze")
    if error:
        return jsonify({"error": error}), 403

    plan_key = current_user.plan if current_user.plan in PLAN_LIMITS else "free"
    limits = PLAN_LIMITS[plan_key]

    game = request.form.get("game", "skyrim").lower().strip()
    if game not in GAME_PROFILES:
        game = "generic"

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
        return jsonify({
            "error": "沒有辨識到模組名稱。請確認是一行一個模組，例如 SkyUI、XPMSSE、Content Patcher、Sodium。",
            "game": game,
            "game_label": GAME_PROFILES[game]["label"]
        }), 400

    if len(mods) > limits["max_mods"]:
        return jsonify({
            "error": f"{limits['name']} 最多支援 {limits['max_mods']} 個模組。你目前輸入 {len(mods)} 個模組。",
            "plan": plan_key,
            "total_mods": len(mods)
        }), 403

    pairs = list(combinations(mods, 2))
    conflict_map, safe_map = batch_fetch_rules(game, pairs)

    conflict_results = []
    safe_results = []
    unknown_results = []
    unknown_pairs = []

    summary = {
        "conflict": 0,
        "safe": 0,
        "unknown": 0,
        "degree_count": {}
    }

    def add_degree_count(degree):
        summary["degree_count"][degree] = summary["degree_count"].get(degree, 0) + 1

    for module_a, module_b in pairs:
        na, nb = normalize_pair(module_a, module_b)
        key = (na.lower(), nb.lower())

        if key in conflict_map:
            item = result_from_conflict(module_a, module_b, conflict_map[key])
            summary["conflict"] += 1
            add_degree_count(item["conflict_degree"])

            if len(conflict_results) < MAX_CONFLICT_RESULTS:
                conflict_results.append(item)

        elif key in safe_map:
            item = result_from_safe(module_a, module_b, safe_map[key])
            summary["safe"] += 1
            add_degree_count(item["conflict_degree"])

            if len(safe_results) < MAX_SAFE_RESULTS:
                safe_results.append(item)

        else:
            summary["unknown"] += 1
            add_degree_count("未知衝突")
            unknown_pairs.append((module_a, module_b))

            if len(unknown_results) < MAX_UNKNOWN_RESULTS:
                unknown_results.append(result_from_unknown(module_a, module_b))

    db.session.add(RawReport(
        game=game,
        user_id=current_user.id,
        report_type=f"{plan_key}_mod_list",
        content=content
    ))

    observe_unknown_pairs(game, unknown_pairs)

    record_usage("analyze")

    # Result order: conflicts first, safe second, then a small unknown sample.
    compact_results = sort_results(conflict_results + safe_results + unknown_results)

    return jsonify({
        "game": game,
        "game_label": GAME_PROFILES[game]["label"],
        "plan": plan_key,
        "plan_name": limits["name"],
        "mods_detected": mods,
        "total_mods": len(mods),
        "total_pairs_checked": len(pairs),
        "displayed_pairs": len(compact_results),
        "response_limited": True,
        "limits": {
            "max_conflict_results": MAX_CONFLICT_RESULTS,
            "max_safe_results": MAX_SAFE_RESULTS,
            "max_unknown_results": MAX_UNKNOWN_RESULTS
        },
        "summary": summary,
        "results": compact_results
    })


@app.route("/api/ai_analyze", methods=["POST"])
@login_required
def api_ai_analyze():
    error = check_usage_or_error("ai")
    if error:
        return jsonify({"error": error}), 403

    if not os.environ.get("OPENAI_API_KEY"):
        return jsonify({"error": "伺服器尚未設定 OPENAI_API_KEY。"}), 500

    plan_key = current_user.plan if current_user.plan in PLAN_LIMITS else "free"
    limits = PLAN_LIMITS[plan_key]

    game = request.form.get("game", "skyrim").lower().strip()
    if game not in GAME_PROFILES:
        game = "generic"

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
        return jsonify({
            "error": "沒有辨識到模組名稱。請確認是一行一個模組。",
            "game": game,
            "game_label": GAME_PROFILES[game]["label"]
        }), 400

    if len(mods) > limits["max_mods"]:
        return jsonify({
            "error": f"{limits['name']} 最多支援 {limits['max_mods']} 個模組。你目前輸入 {len(mods)} 個模組。",
            "total_mods": len(mods)
        }), 403

    # Avoid stale DB connection before a potentially long OpenAI call.
    db.session.commit()
    db.session.close()

    ai_result = call_openai_analysis(game, mods, crash_log)

    # New DB session after OpenAI call.
    db_save_result = save_ai_conflict_result(game, ai_result)

    db.session.add(RawReport(
        game=game,
        user_id=current_user.id,
        report_type="ai_analysis",
        content=content + "\n\n錯誤描述 / Crash Log:\n" + crash_log,
        ai_result=json.dumps(ai_result, ensure_ascii=False)
    ))

    record_usage("ai")

    return jsonify({
        "game": game,
        "game_label": GAME_PROFILES[game]["label"],
        "plan": plan_key,
        "mods_detected": mods,
        "ai_result": ai_result,
        "database_saved": {
            "raw_report_saved": True,
            "conflict_rules_saved": db_save_result["saved_count"],
            "conflict_rules_skipped": db_save_result["skipped_count"],
            "saved_pairs": db_save_result["saved_pairs"],
            "note": "OpenAI 只會把 likely_conflicts 內的衝突組合寫入衝突資料庫；若 AI 判斷沒有明確衝突，則只保存原始 AI 報告。"
        }
    })


@app.route("/api/report_safe", methods=["POST"])
@login_required
def api_report_safe():
    data = request.json or {}
    game = data.get("game", "skyrim").lower().strip()
    if game not in GAME_PROFILES:
        game = "generic"

    module_a = data.get("module_a", "").strip()
    module_b = data.get("module_b", "").strip()

    if not module_a or not module_b:
        return jsonify({"error": "module_a 與 module_b 必填"}), 400

    a, b = normalize_pair(module_a, module_b)

    safe = SafeCombination.query.filter_by(game=game, module_a=a, module_b=b).first()

    if safe:
        safe.report_count += 1
        safe.confidence_score = calculate_confidence(safe.report_count)
    else:
        safe = SafeCombination(
            game=game,
            module_a=a,
            module_b=b,
            report_count=1,
            confidence_score=calculate_confidence(1),
            source="user"
        )
        db.session.add(safe)

    db.session.commit()

    return jsonify({
        "status": "success",
        "message": "安全組合已寫入資料庫。",
        "game": game,
        "module_a": a,
        "module_b": b,
        "report_count": safe.report_count,
        "confidence_score": safe.confidence_score
    })


@app.route("/api/report_conflict", methods=["POST"])
@login_required
def api_report_conflict():
    data = request.json or {}
    game = data.get("game", "skyrim").lower().strip()
    if game not in GAME_PROFILES:
        game = "generic"

    module_a = data.get("module_a", "").strip()
    module_b = data.get("module_b", "").strip()
    conflict_type = data.get("conflict_type", "User Reported Conflict")
    conflict_degree = normalize_conflict_degree(
        data.get("conflict_degree", ""),
        conflict_type
    )

    if not module_a or not module_b:
        return jsonify({"error": "module_a 與 module_b 必填"}), 400

    a, b = normalize_pair(module_a, module_b)

    rule = ConflictRule.query.filter_by(game=game, module_a=a, module_b=b).first()

    if rule:
        rule.report_count += 1
        rule.confidence_score = calculate_confidence(rule.report_count)
        rule.conflict_type = conflict_type
        rule.risk = conflict_degree
        rule.source = "user"
    else:
        rule = ConflictRule(
            game=game,
            module_a=a,
            module_b=b,
            conflict_type=conflict_type,
            risk=conflict_degree,
            report_count=1,
            confidence_score=calculate_confidence(1),
            source="user"
        )
        db.session.add(rule)

    db.session.commit()

    return jsonify({
        "status": "success",
        "message": "衝突回報已寫入資料庫。",
        "game": game,
        "module_a": a,
        "module_b": b,
        "conflict_type": rule.conflict_type,
        "conflict_degree": normalize_conflict_degree(rule.risk, rule.conflict_type),
        "report_count": rule.report_count,
        "confidence_score": rule.confidence_score
    })


@app.route("/api/backfill_auto_safe", methods=["POST"])
@login_required
def api_backfill_auto_safe():
    """
    Backfill old UnknownObservation rows into SafeCombination.

    This fixes older deployments where UnknownObservation.promoted=True was set,
    but the corresponding SafeCombination row was not created.
    """
    data = request.json or {}
    game = (data.get("game") or request.form.get("game") or "skyrim").lower().strip()
    if game not in GAME_PROFILES:
        game = "generic"

    candidates = (
        UnknownObservation.query
        .filter(
            UnknownObservation.game == game,
            UnknownObservation.observe_count >= AUTO_SAFE_THRESHOLD
        )
        .all()
    )

    created_count = 0
    skipped_conflict_count = 0
    skipped_existing_count = 0
    checked_count = 0

    for obs in candidates:
        checked_count += 1

        existing_conflict = ConflictRule.query.filter_by(
            game=game,
            module_a=obs.module_a,
            module_b=obs.module_b
        ).first()

        if existing_conflict:
            skipped_conflict_count += 1
            obs.promoted = True
            continue

        existing_safe = SafeCombination.query.filter_by(
            game=game,
            module_a=obs.module_a,
            module_b=obs.module_b
        ).first()

        if existing_safe:
            skipped_existing_count += 1
            existing_safe.report_count = max(existing_safe.report_count or 1, obs.observe_count)
            existing_safe.confidence_score = max(existing_safe.confidence_score or 0, calculate_confidence(obs.observe_count))
            obs.promoted = True
            continue

        db.session.add(SafeCombination(
            game=game,
            module_a=obs.module_a,
            module_b=obs.module_b,
            report_count=obs.observe_count,
            confidence_score=calculate_confidence(obs.observe_count),
            source="auto_candidate"
        ))
        obs.promoted = True
        created_count += 1

    db.session.commit()

    return jsonify({
        "status": "success",
        "game": game,
        "message": f"已補回 {created_count} 組 auto_candidate 安全組合。",
        "checked_count": checked_count,
        "created_count": created_count,
        "skipped_existing_count": skipped_existing_count,
        "skipped_conflict_count": skipped_conflict_count
    })


@app.route("/api/stats")
def api_stats():
    game = request.args.get("game", "skyrim").lower().strip()
    if game not in GAME_PROFILES:
        game = "generic"

    conflict_total_count = ConflictRule.query.filter_by(game=game).count()
    safe_total_count = SafeCombination.query.filter_by(game=game).count()

    conflicts = (
        ConflictRule.query
        .filter_by(game=game)
        .order_by(ConflictRule.report_count.desc())
        .limit(80)
        .all()
    )

    safes = (
        SafeCombination.query
        .filter_by(game=game)
        .order_by(SafeCombination.report_count.desc())
        .limit(80)
        .all()
    )

    unknown_pending_count = UnknownObservation.query.filter_by(game=game, promoted=False).count()
    unknown_promoted_count = UnknownObservation.query.filter_by(game=game, promoted=True).count()
    unknown_total_count = UnknownObservation.query.filter_by(game=game).count()

    unknown_observation_total = (
        db.session.query(db.func.coalesce(db.func.sum(UnknownObservation.observe_count), 0))
        .filter(UnknownObservation.game == game)
        .scalar()
    )

    unknown_candidates = (
        UnknownObservation.query
        .filter_by(game=game)
        .order_by(UnknownObservation.observe_count.desc())
        .limit(60)
        .all()
    )

    conflict_items = []
    for c in conflicts:
        degree = normalize_conflict_degree(c.risk, c.conflict_type)
        impact = impact_description(c.conflict_type, degree)
        conflict_items.append({
            "module_a": c.module_a,
            "module_b": c.module_b,
            "type": c.conflict_type,
            "conflict_degree": degree,
            "impact_area": impact["impact_area"],
            "effect": impact["effect"],
            "report_count": c.report_count,
            "confidence_score": c.confidence_score,
            "source": c.source
        })

    return jsonify({
        "game": game,
        "game_label": GAME_PROFILES[game]["label"],
        "conflict_total_count": conflict_total_count,
        "safe_total_count": safe_total_count,
        "conflict_rules": conflict_items,
        "safe_combinations": [
            {
                "module_a": s.module_a,
                "module_b": s.module_b,
                "conflict_degree": "無明顯衝突",
                "report_count": s.report_count,
                "confidence_score": s.confidence_score,
                "source": s.source
            }
            for s in safes
        ],
        "unknown_observation_count": unknown_pending_count,
        "unknown_pending_count": unknown_pending_count,
        "unknown_promoted_count": unknown_promoted_count,
        "unknown_total_count": unknown_total_count,
        "unknown_observation_total": int(unknown_observation_total or 0),
        "unknown_candidates": [
            {
                "module_a": u.module_a,
                "module_b": u.module_b,
                "observe_count": u.observe_count,
                "status": "candidate" if u.promoted else "unknown"
            }
            for u in unknown_candidates
        ],
        "plans": PLAN_LIMITS,
        "games": GAME_PROFILES
    })


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
        ("fallout4", "Unofficial Fallout 4 Patch", "LooksMenu")
    ]

    demo_conflicts = [
        ("skyrim", "XPMSSE", "CombatAnimation", "Skeleton Conflict", "高度崩潰風險"),
        ("skyrim", "Serana Dialogue Add-On", "Relationship Dialogue Overhaul", "Dialogue Conflict", "中度功能異常"),
        ("stardew", "Stardew Valley Expanded", "Another Farm Map", "Map / World Conflict", "視覺 / 地圖異常"),
        ("minecraft", "Sodium", "OptiFine", "Loader / Version Conflict", "啟動阻斷"),
        ("cyberpunk", "ArchiveXL", "Old Archive Mod", "Asset Conflict", "視覺 / 地圖異常"),
        ("fallout4", "Full Dialogue Interface", "Dialogue Camera Mod", "Dialogue Conflict", "中度功能異常")
    ]

    for game, a_raw, b_raw in demo_safe:
        a, b = normalize_pair(a_raw, b_raw)
        existing = SafeCombination.query.filter_by(game=game, module_a=a, module_b=b).first()
        if not existing:
            db.session.add(SafeCombination(
                game=game,
                module_a=a,
                module_b=b,
                report_count=8,
                confidence_score=0.90,
                source="demo"
            ))

    for game, a_raw, b_raw, conflict_type, degree in demo_conflicts:
        a, b = normalize_pair(a_raw, b_raw)
        existing = ConflictRule.query.filter_by(game=game, module_a=a, module_b=b).first()
        if not existing:
            db.session.add(ConflictRule(
                game=game,
                module_a=a,
                module_b=b,
                conflict_type=conflict_type,
                risk=degree,
                report_count=12,
                confidence_score=0.95,
                source="demo"
            ))

    db.session.commit()
    return jsonify({"status": "success", "message": "示範資料已建立。"})


with app.app_context():
    ensure_schema()


if __name__ == "__main__":
    app.run(debug=True)
