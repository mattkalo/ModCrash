from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
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
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///clashtest.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")


class ConflictRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    module_a = db.Column(db.String(255), nullable=False)
    module_b = db.Column(db.String(255), nullable=False)
    conflict_type = db.Column(db.String(120), default="Unknown Conflict")
    risk = db.Column(db.String(50), default="Medium")
    report_count = db.Column(db.Integer, default=1)
    confidence_score = db.Column(db.Float, default=0.5)
    source = db.Column(db.String(50), default="user")
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
    ai_result = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def normalize_pair(a, b):
    a = a.replace("*", "").strip()
    b = b.replace("*", "").strip()
    return tuple(sorted([a, b]))


def extract_mods(text):
    mods = []

    for line in text.splitlines():
        line = line.strip().replace("*", "")

        match = re.search(r"[\w\-\s\[\]\(\)']+\.(esp|esm|esl)", line, re.IGNORECASE)

        if match:
            mod_name = match.group(0).strip()
            mods.append(mod_name)

    return sorted(list(set(mods)))


def calculate_confidence(count):
    return round(min(0.35 + count * 0.08, 0.98), 2)


def find_conflict(module_a, module_b):
    a, b = normalize_pair(module_a, module_b)
    return ConflictRule.query.filter_by(module_a=a, module_b=b).first()


def find_safe(module_a, module_b):
    a, b = normalize_pair(module_a, module_b)
    return SafeCombination.query.filter_by(module_a=a, module_b=b).first()


def save_ai_conflict_result(ai_data):
    conflicts = ai_data.get("likely_conflicts", [])

    for item in conflicts:
        module_a = item.get("module_a", "").strip()
        module_b = item.get("module_b", "").strip()

        if not module_a or not module_b:
            continue

        a, b = normalize_pair(module_a, module_b)

        rule = ConflictRule.query.filter_by(module_a=a, module_b=b).first()

        if rule:
            rule.report_count += 1
            rule.confidence_score = calculate_confidence(rule.report_count)
            rule.conflict_type = item.get("conflict_type", rule.conflict_type)
            rule.risk = item.get("risk", rule.risk)
        else:
            rule = ConflictRule(
                module_a=a,
                module_b=b,
                conflict_type=item.get("conflict_type", "AI Predicted Conflict"),
                risk=item.get("risk", "Medium"),
                report_count=1,
                confidence_score=float(item.get("confidence_score", 0.55)),
                source="openai"
            )
            db.session.add(rule)

    db.session.commit()


def call_openai_analysis(mods, crash_log=""):
    prompt = f"""
你是一位遊戲模組相容性分析系統。

請根據以下模組列表與 crash log，分析可能的模組衝突。

要求：
1. 找出最可能衝突的模組組合
2. 判斷衝突類型
3. 判斷風險等級
4. 給玩家具體處理建議
5. 輸出必須是 JSON，不要 Markdown

可用衝突類型：
- Skeleton Conflict
- Script Override
- Dialogue Conflict
- Asset Conflict
- Load Order Conflict
- Dependency Missing
- Unknown Conflict

風險等級：
- High
- Medium
- Low

模組列表：
{mods}

Crash Log：
{crash_log}

請輸出格式：
{{
  "summary": "整體分析摘要",
  "likely_conflicts": [
    {{
      "module_a": "xxx.esp",
      "module_b": "yyy.esp",
      "conflict_type": "Script Override",
      "risk": "High",
      "confidence_score": 0.75,
      "reason": "判斷原因",
      "suggestion": "建議處理方式"
    }}
  ],
  "overall_risk": "Medium"
}}
"""

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "你是專業的遊戲模組衝突分析助理，只輸出 JSON。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2
    )

    content = response.choices[0].message.content

    try:
        return json.loads(content)
    except Exception:
        return {
            "summary": "OpenAI 回傳格式無法解析，但仍保留原始內容。",
            "likely_conflicts": [],
            "overall_risk": "Unknown",
            "raw_output": content
        }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    file = request.files.get("file")

    if not file:
        return jsonify({"error": "沒有收到檔案"}), 400

    content = file.read().decode("utf-8", errors="ignore")
    mods = extract_mods(content)

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
                "report_count": conflict.report_count,
                "source": conflict.source
            })

        elif safe:
            results.append({
                "module_a": module_a,
                "module_b": module_b,
                "status": "safe",
                "conflict_type": "None",
                "risk": "Low",
                "confidence_score": safe.confidence_score,
                "report_count": safe.report_count,
                "source": "user"
            })

        else:
            results.append({
                "module_a": module_a,
                "module_b": module_b,
                "status": "unknown",
                "conflict_type": "Unknown",
                "risk": "Unknown",
                "confidence_score": 0,
                "report_count": 0,
                "source": "none"
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


@app.route("/ai_analyze", methods=["POST"])
def ai_analyze():
    file = request.files.get("file")
    crash_log = request.form.get("crash_log", "")

    if not file:
        return jsonify({"error": "沒有收到檔案"}), 400

    content = file.read().decode("utf-8", errors="ignore")
    mods = extract_mods(content)

    if not os.environ.get("OPENAI_API_KEY"):
        return jsonify({"error": "伺服器尚未設定 OPENAI_API_KEY"}), 500

    ai_result = call_openai_analysis(mods, crash_log)

    db.session.add(RawReport(
        report_type="ai_analysis",
        content=content + "\n\nCrash Log:\n" + crash_log,
        ai_result=json.dumps(ai_result, ensure_ascii=False)
    ))

    save_ai_conflict_result(ai_result)

    return jsonify({
        "mods_detected": mods,
        "ai_result": ai_result
    })


@app.route("/report_conflict", methods=["POST"])
def report_conflict():
    data = request.json

    module_a = data.get("module_a", "").strip()
    module_b = data.get("module_b", "").strip()
    conflict_type = data.get("conflict_type", "User Reported Conflict")
    risk = data.get("risk", "Medium")

    if not module_a or not module_b:
        return jsonify({"error": "module_a 與 module_b 必填"}), 400

    a, b = normalize_pair(module_a, module_b)

    rule = ConflictRule.query.filter_by(module_a=a, module_b=b).first()

    if rule:
        rule.report_count += 1
        rule.confidence_score = calculate_confidence(rule.report_count)
    else:
        rule = ConflictRule(
            module_a=a,
            module_b=b,
            conflict_type=conflict_type,
            risk=risk,
            report_count=1,
            confidence_score=calculate_confidence(1),
            source="user"
        )
        db.session.add(rule)

    db.session.commit()

    return jsonify({
        "status": "success",
        "message": "衝突回報已寫入資料庫",
        "module_a": a,
        "module_b": b,
        "report_count": rule.report_count,
        "confidence_score": rule.confidence_score
    })


@app.route("/report_safe", methods=["POST"])
def report_safe():
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
    conflicts = ConflictRule.query.order_by(ConflictRule.report_count.desc()).limit(50).all()
    safes = SafeCombination.query.order_by(SafeCombination.report_count.desc()).limit(50).all()

    return jsonify({
        "conflict_rules": [
            {
                "module_a": c.module_a,
                "module_b": c.module_b,
                "type": c.conflict_type,
                "risk": c.risk,
                "report_count": c.report_count,
                "confidence_score": c.confidence_score,
                "source": c.source
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


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(debug=True)
