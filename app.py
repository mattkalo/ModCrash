from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from itertools import combinations
from datetime import datetime
from openai import OpenAI
import os
import re
import json

app = Flask(__name__)

database_url = os.environ.get("DATABASE_URL")

if database_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url.replace("postgres://", "postgresql://")
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///modcrash.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")


# =========================
# Game Profiles
# =========================

GAME_PROFILES = {
    "skyrim": {
        "label": "Skyrim / Skyrim Special Edition",
        "extensions": [".esp", ".esm", ".esl"],
        "examples": ["SkyUI", "XPMSSE", "Serana Dialogue Add-On"]
    },
    "fallout4": {
        "label": "Fallout 4",
        "extensions": [".esp", ".esm", ".esl"],
        "examples": ["LooksMenu", "Sim Settlements 2", "Full Dialogue Interface"]
    },
    "stardew": {
        "label": "Stardew Valley",
        "extensions": [".dll", ".json", ".zip"],
        "examples": ["SMAPI", "Content Patcher", "Stardew Valley Expanded"]
    },
    "minecraft": {
        "label": "Minecraft",
        "extensions": [".jar", ".zip"],
        "examples": ["Fabric API", "Sodium", "Create"]
    },
    "cyberpunk": {
        "label": "Cyberpunk 2077",
        "extensions": [".archive", ".xl", ".reds", ".lua"],
        "examples": ["Cyber Engine Tweaks", "ArchiveXL", "TweakXL"]
    },
    "generic": {
        "label": "其他遊戲 / 通用",
        "extensions": [],
        "examples": ["Mod A", "Mod B", "Texture Pack"]
    }
}


# =========================
# Plan Settings
# =========================

PLAN_LIMITS = {
    "free": {
        "name": "免費版",
        "max_mods": 30,
        "allow_ai": False,
        "max_display_pairs": 100
    },
    "pro": {
        "name": "訂閱版",
        "max_mods": 150,
        "allow_ai": True,
        "max_display_pairs": 3000
    }
}


# =========================
# Database Models
# =========================
# 注意：
# risk 欄位保留，避免舊資料庫需要複雜 migration。
# 但內容會改存「衝突程度」，例如：中度功能異常、視覺 / 地圖異常。

class ConflictRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(80), default="skyrim")
    module_a = db.Column(db.String(255), nullable=False)
    module_b = db.Column(db.String(255), nullable=False)
    conflict_type = db.Column(db.String(120), default="Unknown Conflict")
    risk = db.Column(db.String(80), default="中度功能異常")
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RawReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game = db.Column(db.String(80), default="skyrim")
    report_type = db.Column(db.String(50), nullable=False)
    content = db.Column(db.Text, nullable=False)
    ai_result = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# =========================
# Schema Migration Helper
# =========================

def ensure_schema():
    db.create_all()

    inspector = inspect(db.engine)

    table_map = {
        "conflict_rule": "game",
        "safe_combination": "game",
        "raw_report": "game"
    }

    with db.engine.begin() as conn:
        for table_name, column_name in table_map.items():
            if not inspector.has_table(table_name):
                continue

            columns = [col["name"] for col in inspector.get_columns(table_name)]

            if column_name not in columns:
                conn.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        f"ADD COLUMN {column_name} VARCHAR(80) DEFAULT 'skyrim'"
                    )
                )


# =========================
# Request Helpers
# =========================

def get_plan_from_request():
    plan = request.form.get("plan", "free").lower().strip()

    if plan not in PLAN_LIMITS:
        plan = "free"

    return plan


def get_game_from_request():
    game = request.form.get("game", "skyrim").lower().strip()

    if game not in GAME_PROFILES:
        game = "generic"

    return game


def get_game_from_json(data):
    game = data.get("game", "skyrim").lower().strip()

    if game not in GAME_PROFILES:
        game = "generic"

    return game


def is_pro_authorized():
    """
    如果 Render 有設定 PRO_ACCESS_CODE，Pro 就需要輸入相同代碼。
    如果沒有設定 PRO_ACCESS_CODE，則視為展示模式，允許直接使用 Pro。
    """
    required_code = os.environ.get("PRO_ACCESS_CODE")

    if not required_code:
        return True

    input_code = request.form.get("pro_code", "").strip()
    header_code = request.headers.get("X-Pro-Code", "").strip()

    return input_code == required_code or header_code == required_code


# =========================
# Mod Parsing
# =========================

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
        "plugins",
        "plugin list",
        "load order",
        "mod list",
        "active mods",
        "inactive mods",
        "enabled mods",
        "disabled mods",
        "crash log",
        "stack trace",
        "error",
        "warning",
        "----",
        "===="
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


def extract_mods(text, game="generic"):
    """
    支援：
    1. plugins.txt / loadorder.txt
    2. 直接輸入模組名稱
    3. 不同遊戲副檔名
    """
    mods = []
    profile = GAME_PROFILES.get(game, GAME_PROFILES["generic"])
    extensions = profile.get("extensions", [])

    for raw_line in text.splitlines():
        line = normalize_mod_name(raw_line)

        if is_noise_line(line):
            continue

        # 1. 有副檔名的檔名
        if extensions:
            lower = line.lower()
            if any(lower.endswith(ext.lower()) for ext in extensions):
                mods.append(line)
                continue

        # 2. 沒副檔名也接受，一行一個模組名
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
    conflict_type = (conflict_type or "").lower()

    if "dependency" in conflict_type or "missing" in conflict_type:
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

    if "loader" in conflict_type or "version" in conflict_type:
        return "啟動阻斷"

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

    valid_values = [
        "啟動阻斷",
        "高度崩潰風險",
        "中度功能異常",
        "視覺 / 地圖異常",
        "輕微覆蓋",
        "未知衝突",
        "無明顯衝突"
    ]

    if value in legacy_map:
        return legacy_map[value]

    if value in valid_values:
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
            "effect": "可能因遊戲版本、模組載入器或必要函式庫不同，造成遊戲啟動失敗或進入世界時崩潰。"
        }

    if "skeleton" in text_all:
        return {
            "impact_area": "角色骨架 / 動作",
            "effect": "遊戲可能可以啟動，但角色可能出現 T-Pose、動作異常、戰鬥動畫錯誤，嚴重時會崩潰。"
        }

    if "script" in text_all:
        return {
            "impact_area": "任務 / 腳本",
            "effect": "遊戲通常可以執行，但任務可能卡住、功能不觸發、事件失效，長時間遊玩後可能出現存檔污染。"
        }

    if "dialogue" in text_all or "dialog" in text_all:
        return {
            "impact_area": "角色 / 對話",
            "effect": "遊戲通常可以執行，但 NPC 對話、角色互動、任務台詞或多話系統可能出現缺失、重複或錯亂。"
        }

    if (
        "asset" in text_all
        or "texture" in text_all
        or "mesh" in text_all
        or "map" in text_all
        or "world" in text_all
        or conflict_degree == "視覺 / 地圖異常"
    ):
        return {
            "impact_area": "地圖 / 材質 / 模型",
            "effect": "遊戲通常可以執行，但地圖、建築、角色裝備或物件可能出現破圖、紫色材質、模型缺失或碰撞異常。"
        }

    if "load order" in text_all or conflict_degree == "輕微覆蓋":
        return {
            "impact_area": "模組排序 / 覆蓋",
            "effect": "通常不會直接造成閃退，但可能導致部分設定被覆蓋、功能優先順序錯誤或模組效果不明顯。"
        }

    if conflict_degree == "無明顯衝突":
        return {
            "impact_area": "無",
            "effect": "目前資料庫中此組合被回報為可正常共存。"
        }

    return {
        "impact_area": "未知區域",
        "effect": "目前資料不足，僅能判斷此組合可能存在相容性問題，建議使用訂閱版深度分析或提供錯誤描述。"
    }


def find_conflict(game, module_a, module_b):
    a, b = normalize_pair(module_a, module_b)

    return ConflictRule.query.filter_by(
        game=game,
        module_a=a,
        module_b=b
    ).first()


def find_safe(game, module_a, module_b):
    a, b = normalize_pair(module_a, module_b)

    return SafeCombination.query.filter_by(
        game=game,
        module_a=a,
        module_b=b
    ).first()


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
        "source": "user"
    }


def result_from_unknown(module_a, module_b):
    return {
        "module_a": module_a,
        "module_b": module_b,
        "status": "unknown",
        "conflict_type": "Unknown",
        "conflict_degree": "未知衝突",
        "impact_area": "未知",
        "effect": "資料庫尚未累積此組合的足夠資訊。免費版會標記為未知；訂閱版可使用 OpenAI 深度分析。",
        "confidence_score": 0,
        "report_count": 0,
        "source": "none"
    }


def sort_results(results):
    status_priority = {
        "conflict": 0,
        "unknown": 1,
        "safe": 2
    }

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
    summary = {
        "conflict": 0,
        "safe": 0,
        "unknown": 0,
        "degree_count": {}
    }

    for item in results:
        summary[item["status"]] += 1
        degree = item["conflict_degree"]
        summary["degree_count"][degree] = summary["degree_count"].get(degree, 0) + 1

    return summary


def clean_json_text(text):
    text = text.strip()

    if text.startswith("```json"):
        text = text.replace("```json", "", 1).strip()

    if text.startswith("```"):
        text = text.replace("```", "", 1).strip()

    if text.endswith("```"):
        text = text[:-3].strip()

    return text


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
        conflict_degree = (
            item.get("conflict_degree")
            or item.get("risk")
            or default_degree_by_type(conflict_type)
        )
        conflict_degree = normalize_conflict_degree(conflict_degree, conflict_type)

        rule = ConflictRule.query.filter_by(
            game=game,
            module_a=a,
            module_b=b
        ).first()

        if rule:
            rule.report_count += 1
            rule.confidence_score = calculate_confidence(rule.report_count)
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
                confidence_score=float(item.get("confidence_score", 0.55)),
                source="openai"
            )
            db.session.add(rule)

    db.session.commit()


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
1. 玩家可能只提供模組名稱，不一定提供 .esp、.esm、.jar、.dll 等副檔名。
2. 請依照目前遊戲的模組生態判斷衝突。
3. 如果是 Skyrim / Fallout，常見衝突包含 load order、script、skeleton、dialogue、asset。
4. 如果是 Stardew Valley，常見衝突包含 SMAPI、Content Patcher、地圖覆蓋、事件腳本、NPC 對話。
5. 如果是 Minecraft，常見衝突包含 loader 不相容、library 缺失、mixin crash、版本不符、渲染模組衝突。
6. 如果是 Cyberpunk 2077，常見衝突包含 ArchiveXL、TweakXL、REDscript、CET、材質或腳本覆蓋。
7. 如果資訊不足，請明確說明需要更多資料，不要捏造不存在的 crash log。

要求：
1. 找出最可能衝突的模組組合
2. 判斷衝突類型
3. 判斷衝突程度，不要使用 High / Medium / Low
4. 描述玩家實際會遇到的影響
5. 給玩家具體處理建議
6. 輸出必須是 JSON，不要 Markdown

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

衝突程度說明：
- 啟動阻斷：遊戲可能無法啟動，或主選單 / 讀檔前崩潰
- 高度崩潰風險：遊戲可啟動，但讀檔、戰鬥、傳送、切換場景時可能崩潰
- 中度功能異常：遊戲可執行，但角色、任務、對話、腳本、多話功能可能異常
- 視覺 / 地圖異常：遊戲可執行，但地圖、模型、材質、碰撞可能破圖或缺失
- 輕微覆蓋：通常可玩，但部分設定或模組效果可能被覆蓋
- 未知衝突：資訊不足

模組列表：
{json.dumps(mods, ensure_ascii=False)}

錯誤描述 / Crash Log：
{crash_log}

請輸出格式：
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
            {
                "role": "system",
                "content": "你是專業的遊戲模組衝突分析助理，只輸出 JSON。"
            },
            {
                "role": "user",
                "content": prompt
            }
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


# =========================
# Routes
# =========================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    plan = get_plan_from_request()
    game = get_game_from_request()
    limits = PLAN_LIMITS[plan]

    if plan == "pro" and not is_pro_authorized():
        return jsonify({"error": "訂閱版驗證碼錯誤，或尚未開通訂閱版。"}), 403

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
            "error": f"{limits['name']} 最多支援 {limits['max_mods']} 個模組。你目前上傳 {len(mods)} 個模組。",
            "plan": plan,
            "total_mods": len(mods)
        }), 403

    results = []

    for module_a, module_b in combinations(mods, 2):
        conflict = find_conflict(game, module_a, module_b)
        safe = find_safe(game, module_a, module_b)

        if conflict:
            results.append(result_from_conflict(module_a, module_b, conflict))
        elif safe:
            results.append(result_from_safe(module_a, module_b, safe))
        else:
            results.append(result_from_unknown(module_a, module_b))

    results = sort_results(results)
    summary = summarize_results(results)

    db.session.add(RawReport(
        game=game,
        report_type=f"{plan}_mod_list",
        content=content
    ))

    db.session.commit()

    display_results = results[:limits["max_display_pairs"]]

    return jsonify({
        "game": game,
        "game_label": GAME_PROFILES[game]["label"],
        "plan": plan,
        "plan_name": limits["name"],
        "mods_detected": mods,
        "total_mods": len(mods),
        "total_pairs_checked": len(results),
        "displayed_pairs": len(display_results),
        "summary": summary,
        "results": display_results
    })


@app.route("/ai_analyze", methods=["POST"])
def ai_analyze():
    plan = get_plan_from_request()
    game = get_game_from_request()

    if plan != "pro":
        return jsonify({
            "error": "OpenAI 深度分析是訂閱版功能。免費版只能使用資料庫快速分析。"
        }), 403

    if not is_pro_authorized():
        return jsonify({"error": "訂閱版驗證碼錯誤，或尚未開通訂閱版。"}), 403

    if not os.environ.get("OPENAI_API_KEY"):
        return jsonify({"error": "伺服器尚未設定 OPENAI_API_KEY。"}), 500

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
            "error": "沒有辨識到模組名稱。請確認是一行一個模組，例如 SkyUI、XPMSSE、Content Patcher、Sodium。",
            "game": game,
            "game_label": GAME_PROFILES[game]["label"]
        }), 400

    limits = PLAN_LIMITS["pro"]

    if len(mods) > limits["max_mods"]:
        return jsonify({
            "error": f"訂閱版最多支援 {limits['max_mods']} 個模組。你目前上傳 {len(mods)} 個模組。",
            "total_mods": len(mods)
        }), 403

    ai_result = call_openai_analysis(game, mods, crash_log)

    db.session.add(RawReport(
        game=game,
        report_type="pro_ai_analysis",
        content=content + "\n\n錯誤描述 / Crash Log:\n" + crash_log,
        ai_result=json.dumps(ai_result, ensure_ascii=False)
    ))

    save_ai_conflict_result(game, ai_result)

    return jsonify({
        "game": game,
        "game_label": GAME_PROFILES[game]["label"],
        "plan": "pro",
        "mods_detected": mods,
        "ai_result": ai_result
    })


@app.route("/report_conflict", methods=["POST"])
def report_conflict():
    data = request.json or {}

    game = get_game_from_json(data)

    module_a = data.get("module_a", "").strip()
    module_b = data.get("module_b", "").strip()
    conflict_type = data.get("conflict_type", "User Reported Conflict")
    conflict_degree = data.get("conflict_degree", "")
    conflict_degree = normalize_conflict_degree(conflict_degree, conflict_type)

    if not conflict_degree or conflict_degree == "Unknown":
        conflict_degree = default_degree_by_type(conflict_type)

    if not module_a or not module_b:
        return jsonify({"error": "module_a 與 module_b 必填"}), 400

    a, b = normalize_pair(module_a, module_b)

    rule = ConflictRule.query.filter_by(
        game=game,
        module_a=a,
        module_b=b
    ).first()

    if rule:
        rule.report_count += 1
        rule.confidence_score = calculate_confidence(rule.report_count)
        rule.conflict_type = conflict_type
        rule.risk = conflict_degree
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

    degree = normalize_conflict_degree(rule.risk, rule.conflict_type)
    impact = impact_description(rule.conflict_type, degree)

    return jsonify({
        "status": "success",
        "message": "衝突回報已寫入資料庫。",
        "game": game,
        "game_label": GAME_PROFILES[game]["label"],
        "module_a": a,
        "module_b": b,
        "conflict_type": rule.conflict_type,
        "conflict_degree": degree,
        "impact_area": impact["impact_area"],
        "effect": impact["effect"],
        "report_count": rule.report_count,
        "confidence_score": rule.confidence_score
    })


@app.route("/report_safe", methods=["POST"])
def report_safe():
    data = request.json or {}

    game = get_game_from_json(data)

    module_a = data.get("module_a", "").strip()
    module_b = data.get("module_b", "").strip()

    if not module_a or not module_b:
        return jsonify({"error": "module_a 與 module_b 必填"}), 400

    a, b = normalize_pair(module_a, module_b)

    safe = SafeCombination.query.filter_by(
        game=game,
        module_a=a,
        module_b=b
    ).first()

    if safe:
        safe.report_count += 1
        safe.confidence_score = calculate_confidence(safe.report_count)
    else:
        safe = SafeCombination(
            game=game,
            module_a=a,
            module_b=b,
            report_count=1,
            confidence_score=calculate_confidence(1)
        )
        db.session.add(safe)

    db.session.commit()

    return jsonify({
        "status": "success",
        "message": "安全組合已寫入資料庫。",
        "game": game,
        "game_label": GAME_PROFILES[game]["label"],
        "module_a": a,
        "module_b": b,
        "conflict_degree": "無明顯衝突",
        "report_count": safe.report_count,
        "confidence_score": safe.confidence_score
    })


@app.route("/stats")
def stats():
    game = request.args.get("game", "skyrim").lower().strip()

    if game not in GAME_PROFILES:
        game = "generic"

    conflicts = (
        ConflictRule.query
        .filter_by(game=game)
        .order_by(ConflictRule.report_count.desc())
        .limit(50)
        .all()
    )

    safes = (
        SafeCombination.query
        .filter_by(game=game)
        .order_by(SafeCombination.report_count.desc())
        .limit(50)
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
        "conflict_rules": conflict_items,
        "safe_combinations": [
            {
                "module_a": s.module_a,
                "module_b": s.module_b,
                "conflict_degree": "無明顯衝突",
                "report_count": s.report_count,
                "confidence_score": s.confidence_score
            }
            for s in safes
        ],
        "plans": PLAN_LIMITS,
        "games": GAME_PROFILES
    })


with app.app_context():
    ensure_schema()


if __name__ == "__main__":
    app.run(debug=True)
