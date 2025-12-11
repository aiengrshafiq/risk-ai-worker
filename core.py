# core.py
# All helper logic used by index.handler
# IMPORTANT: No feature calculations here, only reading and decisions.

import json
import time
import psycopg2
from psycopg2 import extensions as psyext
import urllib.request
import urllib.error
from datetime import datetime, timezone   # <-- ADD THIS

import config as cfg



print("[RISK_FC] Loading core.py")

_RULES_CACHE     = None
_LAST_CACHE_TIME = 0

# NEW: global DB connection (reused per FC instance)
_DB_CONN = None

# ==========================
# DB HELPERS
# ==========================
def get_db_conn():
    """
    Reuse a single psycopg2 connection per FC instance and
    auto-heal if the previous transaction is in error.
    """
    global _DB_CONN

    # If we already have a connection, validate it
    if _DB_CONN is not None:
        try:
            if _DB_CONN.closed == 0:
                status = _DB_CONN.get_transaction_status()

                if status == psyext.TRANSACTION_STATUS_INERROR:
                    # Previous statement failed; we must rollback before reuse
                    print("[RISK_FC] DB connection in error state, rolling back transaction.")
                    _DB_CONN.rollback()
                    return _DB_CONN

                if status == psyext.TRANSACTION_STATUS_UNKNOWN:
                    # Connection is broken; drop and recreate
                    print("[RISK_FC] DB connection status UNKNOWN, closing and recreating.")
                    try:
                        _DB_CONN.close()
                    except Exception:
                        pass
                    _DB_CONN = None
                else:
                    # OK to reuse
                    return _DB_CONN
            else:
                _DB_CONN = None
        except Exception as e:
            print(f"[RISK_FC] get_db_conn status check failed: {e}")
            try:
                _DB_CONN.close()
            except Exception:
                pass
            _DB_CONN = None

    # Create a new connection if we reach here
    print("[RISK_FC] Establishing NEW DB connection to Hologres...")
    _DB_CONN = psycopg2.connect(
        host=cfg.DB_HOST,
        port=cfg.DB_PORT,
        database=cfg.DB_NAME,
        user=cfg.DB_USER,
        password=cfg.DB_PASS,
        connect_timeout=3,
    )
    return _DB_CONN


def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col.name] = row[idx]
    return d


def fetch_risk_features(user_code, txn_id):
    """
    Fetch a single row from rt.risk_features for (user_code, txn_id).

    Uses the global connection from get_db_conn() and only closes the cursor,
    not the connection.
    """
    try:
        conn = get_db_conn()
        cur  = conn.cursor()
        sql  = "SELECT * FROM rt.risk_features WHERE user_code = %s AND txn_id = %s"
        cur.execute(sql, (str(user_code), str(txn_id)))
        row = cur.fetchone()
        if row:
            result = dict_factory(cur, row)
        else:
            result = None
        cur.close()
        return result
    except Exception as exc:
        print(f"[RISK_FC] Error fetching features: {exc}")
        return None


def patch_withdrawal_ratio(features):
    amt   = features.get("withdrawal_amount")
    ratio = features.get("withdrawal_ratio")
    total = features.get("total_balance_sum")

    try:
        amt   = float(amt) if amt is not None else None
        ratio = float(ratio) if ratio is not None else None
        total = float(total) if total is not None else None
    except:
        return features

    # If total balance is clearly nonsense, treat ratio as 1.0 but mark it as uncertain
    if amt is not None and (total is None or total <= 0):
        features["withdrawal_ratio"] = 1.0
        features["withdrawal_ratio_source"] = "UNKNOWN_BALANCE"
        return features

    # If total is much smaller than withdrawal, also treat as likely full drain
    if amt is not None and total is not None and total < amt * 0.1:
        features["withdrawal_ratio"] = 1.0
        features["withdrawal_ratio_source"] = "SUSPICIOUS_TOTAL_BALANCE"
        return features

    features["withdrawal_ratio_source"] = "BALANCE_CACHE"
    return features

def refresh_sanctions_and_age(features, max_wait=5, delay=0.2):
    """
    Try to re-read dim_sanctions_address + dim_destination_age
    for a short period if status is not CHECKED yet.

    External APIs are called by the enrichment worker and write into these tables.
    Here we only poll Hologres to see if enrichment is finished.

    max_wait = number of attempts
    delay    = sleep between attempts (seconds)
    """
    chain   = features.get("chain")
    address = features.get("destination_address")
    if not chain or not address:
        return features

    # If both already CHECKED, nothing to do
    if features.get("sanctions_status") == "CHECKED" and features.get("age_status") == "CHECKED":
        return features

    try:
        conn = get_db_conn()
        cur  = conn.cursor()

        for attempt in range(max_wait):
            cur.execute(
                """
                SELECT is_sanctioned, sanctions_status
                FROM rt.dim_sanctions_address
                WHERE chain = %s AND destination_address = %s
                """,
                (chain, address),
            )
            sanc_row = cur.fetchone()

            cur.execute(
                """
                SELECT destination_age_hours, age_status
                FROM rt.dim_destination_age
                WHERE chain = %s AND destination_address = %s
                """,
                (chain, address),
            )
            age_row = cur.fetchone()

            updated = False

            if sanc_row:
                is_sanctioned, sanctions_status = sanc_row
                if sanctions_status == "CHECKED":
                    features["is_sanctioned"]    = bool(is_sanctioned)
                    features["sanctions_status"] = sanctions_status
                    updated = True

            if age_row:
                dest_age_hours, age_status = age_row
                if age_status == "CHECKED":
                    features["destination_age_hours"] = int(dest_age_hours)
                    features["age_status"]            = age_status
                    updated = True

            if updated:
                print("[RISK_FC] Inline sanctions/age refresh succeeded")
                break

            print(
                f"[RISK_FC] Sanctions/age not ready yet for {chain}:{address} "
                f"(attempt {attempt+1}/{max_wait}), sleeping {delay}s"
            )
            time.sleep(delay)

        cur.close()
    except Exception as e:
        print("[RISK_FC] refresh_sanctions_and_age error:", e)

    return features


def wait_for_risk_features(user_code, txn_id, max_retries=5, delay=0.2):
    """
    Wait briefly for rt.risk_features to be populated for (user_code, txn_id).

    max_retries=3, delay=0.2 → max wait ~0.6s.
    """
    for attempt in range(max_retries):
        features = fetch_risk_features(user_code, txn_id)
        if features:
            if attempt > 0:
                print(
                    f"[RISK_FC] risk_features found on attempt {attempt+1}/{max_retries}"
                )
            return features

        print(
            f"[RISK_FC] risk_features NOT found yet for user_code={user_code}, txn_id={txn_id} "
            f"(attempt {attempt+1}/{max_retries}), sleeping {delay}s"
        )
        time.sleep(delay)

    print(
        f"[RISK_FC] risk_features still missing after {max_retries} attempts for "
        f"user_code={user_code}, txn_id={txn_id}"
    )
    return None


# ==========================
# LARK NOTIFICATION
# ==========================
def send_lark_notification(data):
    """
    NO-OP in AI worker.

    The async AI worker never sends Lark notifications.
    Lark alerts are only sent by the main risk decision FC.
    """
    print("[AI_WORKER] Lark notification suppressed for decision:",
          data.get("decision", "UNKNOWN"))

# ==========================
# RULES CACHE & DECISION LOGGING
# ==========================
def load_dynamic_rules():
    """
    Load ACTIVE rules from rt.risk_rules with an in-process cache.
    Uses a shared DB connection and only closes the cursor.
    """
    global _RULES_CACHE, _LAST_CACHE_TIME
    if _RULES_CACHE is not None and (time.time() - _LAST_CACHE_TIME < cfg.RULE_CACHE_TTL):
        return _RULES_CACHE

    try:
        conn = get_db_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT * FROM rt.risk_rules WHERE status = 'ACTIVE' ORDER BY priority ASC"
        )
        rows  = cur.fetchall()
        rules = []
        if rows:
            for row in rows:
                rules.append(dict_factory(cur, row))

        cur.close()
        _RULES_CACHE     = rules
        _LAST_CACHE_TIME = time.time()
        return rules
    except Exception as exc:
        print(f"[RISK_FC] Error loading rules: {exc}")
        return _RULES_CACHE if _RULES_CACHE else []


def log_decision_to_db(
    user_code,
    txn_id,
    result,
    features,
    source,
    processing_time_ms=None,
):
    """
    Insert a decision into rt.risk_withdraw_decision using the
    global DB connection. Only closes the cursor.
    """
    try:
        conn = get_db_conn()
        cur  = conn.cursor()
        sql  = """
            INSERT INTO rt.risk_withdraw_decision 
            (user_code, txn_id, decision, primary_threat, confidence, narrative, 
             features_snapshot, decision_source, llm_reasoning, processing_time_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        decision      = result.get("decision", "HOLD")
        threat        = result.get("primary_threat", "UNKNOWN")
        narrative     = result.get("narrative", "")
        llm_reasoning = narrative

        # Prefer explicit confidence if provided, else derive from risk_score
        if "confidence" in result:
            try:
                confidence = float(result.get("confidence"))
            except Exception:
                confidence = 0.7
        else:
            score = result.get("risk_score", 0)
            if isinstance(score, (int, float)) and score >= 0:
                confidence = float(score) / 100.0
            else:
                confidence = 1.0

        features_json = json.dumps(features, default=str)

        cur.execute(
            sql,
            (
                str(user_code),
                str(txn_id),
                decision,
                threat,
                confidence,
                narrative,
                features_json,
                source,
                llm_reasoning,
                processing_time_ms,
            ),
        )
        conn.commit()
        cur.close()
        print(f"[RISK_FC] Decision logged. Source: {source}, decision={decision}")
    except Exception as exc:
        print(f"[RISK_FC] Error logging decision: {exc}")



# ==========================
# RULE EVALUATION (rt.risk_rules)
# ==========================
def evaluate_fixed_rules(features, rules):
    """
    Evaluate rules from rt.risk_rules based purely on features in rt.risk_features.
    """
    safe_locals = {}
    for k, v in features.items():
        safe_locals[k] = 0 if v is None else v

    for rule in rules:
        try:
            logic = rule["logic_expression"].replace('\n', ' ').replace('\r', '').strip()
            if eval(logic, {"__builtins__": None}, safe_locals):
                print(f"[RISK_FC] Rule HIT: {rule.get('rule_name')}")
                return {
                    "triggered": True,
                    "decision": rule["action"],  # PASS / HOLD / REJECT
                    "primary_threat": "RULE_HIT",
                    "risk_score": 100,
                    "narrative": f"[Rule #{rule.get('rule_id')}] {rule.get('narrative')}",
                    # NEW: pass rule metadata to AI
                    "rule_id": rule.get("rule_id"),
                    "rule_name": rule.get("rule_name"),
                }
        except Exception as exc:
            print(f"[RISK_FC] Error evaluating rule '{rule.get('rule_name')}': {exc}")
            continue
    return {"triggered": False}


# ==========================
# AI AGENT
# ==========================
def call_gemini_reasoning_rest(features, rule_context=None):
    """
    Phase-2 AI Agent.

    Uses a fast Gemini model (e.g. gemini-2.5-flash) with:
    - Fewer retries
    - Shorter timeout
    """
    if not cfg.GEMINI_API_KEY:
        return {
            "final_decision": "HOLD",
            "primary_threat": "NONE",
            "risk_score": 0,
            "confidence": 0.5,
            "narrative": "AI config missing. Keeping HOLD for manual review.",
            "rule_alignment": "AGREES_WITH_RULE",
        }

    try:
        case_payload = {
            "features": features,
            "rule_engine": {
                "initial_decision": (rule_context or {}).get("decision", "HOLD"),
                "rule_id": (rule_context or {}).get("rule_id"),
                "rule_name": (rule_context or {}).get("rule_name"),
                "rule_narrative": (rule_context or {}).get("narrative"),
            },
        }

        case_str         = json.dumps(case_payload, default=str)
        full_text_prompt = f"{cfg.COMPREHENSIVE_REASONING_PROMPT}\n\nCase JSON:\n{case_str}"

        api_url = (
            f"https://generativelanguage.googleapis.com/v1/models/"
            f"{cfg.GEMINI_MODEL}:generateContent?key={cfg.GEMINI_API_KEY}"
        )
        payload = {"contents": [{"parts": [{"text": full_text_prompt}]}]}
        data    = json.dumps(payload).encode("utf-8")
        req     = urllib.request.Request(
            api_url, data=data, headers={"Content-Type": "application/json"}
        )

        # Reduced retries and timeout for latency
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    if response.status != 200:
                        print(f"[RISK_FC] Gemini HTTP status {response.status}")
                        break

                    resp_json  = json.loads(response.read().decode("utf-8"))
                    candidates = resp_json.get("candidates", [])
                    if not candidates:
                        print("[RISK_FC] Gemini: no candidates returned")
                        break

                    raw_text = (
                        candidates[0]
                        .get("content", {})
                        .get("parts", [])[0]
                        .get("text", "")
                    )
                    clean_text = (
                        raw_text.strip()
                        .replace("```json", "")
                        .replace("```", "")
                        .strip()
                    )
                    ai_obj = json.loads(clean_text)

                    final_decision = ai_obj.get("final_decision", "HOLD")
                    primary_threat = ai_obj.get("primary_threat", "NONE")
                    risk_score     = int(ai_obj.get("risk_score", 0) or 0)
                    confidence     = float(ai_obj.get("confidence", 0.7) or 0.7)
                    narrative      = ai_obj.get("narrative", "AI evaluation.")
                    rule_alignment = ai_obj.get("rule_alignment", "AGREES_WITH_RULE")

                    return {
                        "final_decision": final_decision,
                        "primary_threat": primary_threat,
                        "risk_score": risk_score,
                        "confidence": confidence,
                        "narrative": narrative,
                        "rule_alignment": rule_alignment,
                    }
            except urllib.error.HTTPError as e:
                print(f"[RISK_FC] HTTP Error (Gemini): {e.code}")
                time.sleep(0.5)
            except Exception as e:
                print(f"[RISK_FC] Gemini error attempt {attempt+1}: {e}")
                time.sleep(0.5)

        # Fallback if all attempts fail or JSON is bad
        return {
            "final_decision": "HOLD",
            "primary_threat": "AI_NET_ERR",
            "risk_score": -1,
            "confidence": 0.5,
            "narrative": "AI unavailable or invalid response. Keeping HOLD for manual review.",
            "rule_alignment": "AGREES_WITH_RULE",
        }
    except Exception as exc:
        print(f"[RISK_FC] Gemini fatal error: {exc}")
        return {
            "final_decision": "HOLD",
            "primary_threat": "AI_ERR",
            "risk_score": -1,
            "confidence": 0.5,
            "narrative": f"AI exception: {str(exc)}",
            "rule_alignment": "AGREES_WITH_RULE",
        }


