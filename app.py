from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from itertools import combinations
from datetime import datetime
import os
import re

app = Flask(__name__)

database_url = os.environ.get("DATABASE_URL")

if database_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url.replace("postgres://", "postgresql://")
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///clashtest.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# =========================
# Database Models
# =========================

class ConflictRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    module_a = db.Column(db.String(255), nullable=False)
    module_b = db.Column(db.String(255), nullable=False)
    conflict_type = db.Column(db.String(120), default="Unknown Conflict")
    risk = db.Column(db.String(50), default="Medium")
    report_count = db.Column(db.Integer, default=1)
    confidence_score = db.Column(db.Float, default=0.5)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SafeCombination(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    module_a = db.Column(db.String(255), nullable=False)
    module_b = db.Column(db.String(255), nullable=False)
    report_count = db.Column(db.Integer, default=1)
    confidence_score = db.Column(db.Float, default=0.5)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RawReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_type = db.Column(db.String(50), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# =========================
# Utility Functions
# =========================

def normalize_pair(a, b):
    a = a.strip()
    b = b.strip()
    return tuple(sorted([a, b]))


def extract_mods(text):
    """
    從 plugins.txt / crash log 中抓出 .esp / .esm / .esl 模組名稱
    """
    pattern = r"[\w\-\s\[\]\(\)]+?\.(esp|esm|esl)"
    matches = re.findall(pattern, text, flags=re.IGNORECASE)

    mods = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().endswith((".esp", ".esm", ".esl")):
            clean = line.replace("*", "").strip()
            mods.append(clean)

    # 去重
    return sorted(list(set(mods)))


def guess_conflict_type(text):
    lower = text.lower()

    if "skeleton" in lower or "bone" in lower:
        return "Skeleton Conflict", "High"

    if "script" in lower or "papyrus" in lower:
        return "Script Override", "Medium"

    if "dialogue" in lower or "dialog" in lower:
        return "Dialogue Conflict", "Medium"

    if "texture" in lower or "mesh" in lower or "asset" in lower:
        return "Asset Conflict", "Low"

    if "load order" in lower or "sort" in lower:
        return "Load Order Conflict", "Medium"

    return "Unknown Conflict", "Medium"


def calculate_confidence(count):
    """
    回報次數越多，信任分數越高
    """
    score = min(0.35 + count * 0.08, 0.98)
    return round(score, 2)


def find_conflict(module_a, module_b):
    a, b = normalize_pair(module_a, module_b)

    return ConflictRule.query.filter_by(
        module_a=a,
        module_b=b
    ).first()


def find_safe(module_a, module_b):
    a, b = normalize_pair(module_a, module_b)

    return SafeCombination.query.filter_by(
        module_a=a,
        module_b=b
    ).first()


# =========================
# Routes
# =========================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    """
    玩家只上傳模組列表，系統判斷：
    1. 已知衝突
    2. 已知安全
    3. 未知組合
    """
    file = request.files.get("file")

    if not file:
        return jsonify({"error": "沒有收到檔案"}), 400

    content = file.read().decode("utf-8", errors="ignore")
    mods = extract_mods(content)

    RawReport(
        report_type="mod_list",
        content=content
    )

    results = []

    for module_a, module_b in combinations(mods, 2):
        conflict = find_conflict(module_a, module_b)
        safe = find_safe(module_a, module_b)

        if conflict:
            results.append({
                "module_a": module_a,
                "module_b": module_b,
                "status": "conflict",
                "conflict_type": conflict.conflict_type,
                "risk": conflict.risk,
                "confidence_score": conflict.confidence_score,
                "report_count": conflict.report_count
            })

        elif safe:
            results.append({
                "module_a": module_a,
                "module_b": module_b,
                "status": "safe",
                "conflict_type": "None",
                "risk": "Low",
                "confidence_score": safe.confidence_score,
                "report_count": safe.report_count
            })

        else:
            results.append({
                "module_a": module_a,
                "module_b": module_b,
                "status": "unknown",
                "conflict_type": "Unknown",
                "risk": "Unknown",
                "confidence_score": 0,
                "report_count": 0
            })

    db.session.add(RawReport(
        report_type="mod_list",
        content=content
    ))
    db.session.commit()

    return jsonify({
        "mods_detected": mods,
        "total_mods": len(mods),
        "total_pairs_checked": len(results),
        "results": results
    })


@app.route("/report_conflict", methods=["POST"])
def report_conflict():
    """
    玩家回報：這兩個模組會衝突
    """
    data = request.json

    module_a = data.get("module_a", "").strip()
    module_b = data.get("module_b", "").strip()
    crash_log = data.get("crash_log", "")

    if not module_a or not module_b:
        return jsonify({"error": "module_a 與 module_b 必填"}), 400

    a, b = normalize_pair(module_a, module_b)
    conflict_type, risk = guess_conflict_type(crash_log)

    rule = ConflictRule.query.filter_by(module_a=a, module_b=b).first()

    if rule:
        rule.report_count += 1
        rule.confidence_score = calculate_confidence(rule.report_count)
        rule.conflict_type = conflict_type
        rule.risk = risk
    else:
        rule = ConflictRule(
            module_a=a,
            module_b=b,
            conflict_type=conflict_type,
            risk=risk,
            report_count=1,
            confidence_score=calculate_confidence(1)
        )
        db.session.add(rule)

    db.session.add(RawReport(
        report_type="conflict_report",
        content=crash_log
    ))

    db.session.commit()

    return jsonify({
        "status": "success",
        "message": "衝突回報已寫入資料庫",
        "module_a": a,
        "module_b": b,
        "conflict_type": rule.conflict_type,
        "risk": rule.risk,
        "report_count": rule.report_count,
        "confidence_score": rule.confidence_score
    })


@app.route("/report_safe", methods=["POST"])
def report_safe():
    """
    玩家回報：這兩個模組可正常共存
    """
    data = request.json

    module_a = data.get("module_a", "").strip()
    module_b = data.get("module_b", "").strip()

    if not module_a or not module_b:
        return jsonify({"error": "module_a 與 module_b 必填"}), 400

    a, b = normalize_pair(module_a, module_b)

    safe = SafeCombination.query.filter_by(module_a=a, module_b=b).first()

    if safe:
        safe.report_count += 1
        safe.confidence_score = calculate_confidence(safe.report_count)
    else:
        safe = SafeCombination(
            module_a=a,
            module_b=b,
            report_count=1,
            confidence_score=calculate_confidence(1)
        )
        db.session.add(safe)

    db.session.commit()

    return jsonify({
        "status": "success",
        "message": "安全組合已寫入資料庫",
        "module_a": a,
        "module_b": b,
        "report_count": safe.report_count,
        "confidence_score": safe.confidence_score
    })


@app.route("/stats")
def stats():
    conflicts = ConflictRule.query.order_by(ConflictRule.report_count.desc()).all()
    safes = SafeCombination.query.order_by(SafeCombination.report_count.desc()).all()

    return jsonify({
        "conflict_rules": [
            {
                "module_a": c.module_a,
                "module_b": c.module_b,
                "type": c.conflict_type,
                "risk": c.risk,
                "report_count": c.report_count,
                "confidence_score": c.confidence_score
            }
            for c in conflicts
        ],
        "safe_combinations": [
            {
                "module_a": s.module_a,
                "module_b": s.module_b,
                "report_count": s.report_count,
                "confidence_score": s.confidence_score
            }
            for s in safes
        ]
    })


@app.before_request
def create_tables():
    db.create_all()


if __name__ == "__main__":
    app.run(debug=True)
