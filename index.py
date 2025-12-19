# ai_worker.py
# Async AI worker for Phase-2 risk decisions.
#
# Deploy this as a separate FC function with a Timer/Cron trigger (e.g. every 30s).
# It scans for RULE_ENGINE_RULES HOLD decisions that do NOT yet have an
# AI_AGENT_REVIEW row, then calls Gemini and logs the final AI decision.

import json
import time

import core  # reuse DB, Gemini, logging, Lark, etc.


def _fetch_pending_hold_txns(limit=50):
    """
    Return a list of (user_code, txn_id) that:
      - Have a RULE_ENGINE_RULES HOLD decision
      - Do NOT yet have an AI_AGENT_REVIEW decision for same (user_code, txn_id)

    We group by (user_code, txn_id) and order by the earliest decision_timestamp.
    """
    try:
        conn = core.get_db_conn()
        cur  = conn.cursor()

        sql = """
            SELECT sub.user_code, sub.txn_id
            FROM (
                SELECT
                    d.user_code,
                    d.txn_id,
                    MIN(d.decision_timestamp) AS first_decision_ts
                FROM rt.risk_withdraw_decision d
                WHERE d.decision_source = 'RULE_ENGINE_RULES'
                  AND d.decision = 'HOLD'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM rt.risk_withdraw_decision a
                    WHERE a.user_code = d.user_code
                      AND a.txn_id    = d.txn_id
                      AND a.decision_source = 'AI_AGENT_REVIEW'
                  )
                GROUP BY d.user_code, d.txn_id
            ) sub
            ORDER BY sub.first_decision_ts ASC
            LIMIT %s
        """

        cur.execute(sql, (limit,))
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception as e:
        print(f"[AI_WORKER] Error fetching pending HOLD txns: {e}")
        # In error cases, return empty so this run does nothing
        return []


def _process_single_txn(user_code, txn_id):
    print(f"[AI_WORKER] Processing txn user_code={user_code}, txn_id={txn_id}")

    # 1) Fetch features
    features = core.fetch_risk_features(user_code, txn_id)
    if not features:
        print(f"[AI_WORKER] No risk_features for user_code={user_code}, txn_id={txn_id}, skipping.")
        return False

    # Ensure user_code/txn_id in snapshot
    features["user_code"] = user_code
    features["txn_id"]    = txn_id

    withdrawal_amount = features.get("withdrawal_amount")
    withdraw_currency = features.get("withdraw_currency") or features.get("withdrawal_currency")

    # 2) Refresh sanctions/age (short polling)
    features = core.refresh_sanctions_and_age(features, max_wait=5, delay=0.2)

    #2.1 patch_withdrawal_ratio
    features = core.patch_withdrawal_ratio(features)

    # 3) Load rules & get context (to pass rule metadata into AI)
    rules        = core.load_dynamic_rules()
    rule_context = core.evaluate_fixed_rules(features, rules)
    if not rule_context.get("triggered"):
        # In theory, we already know RULE_ENGINE was HOLD, but if eval fails, just send a minimal context.
        #rule_context = {"decision": "HOLD"}
        phase1_narr = core.fetch_phase1_hold_narrative(user_code, txn_id)
        rule_context = {"decision": "HOLD", "narrative": phase1_narr} if phase1_narr else {"decision":"HOLD"}

    # 3.1) Build rich behavior context (single query)
    behavior_context = core.build_behavior_context(user_code, features)


    # 4) Call Gemini
    t0     = time.perf_counter()
    ai_raw = core.call_gemini_reasoning_rest(features, rule_context=rule_context,
        behavior_context=behavior_context)
    t1     = time.perf_counter()
    ai_ms  = int((t1 - t0) * 1000)

    final_decision      = ai_raw.get("final_decision", "HOLD")
    final_primary_threat = ai_raw.get("primary_threat", "NONE")
    final_risk_score    = ai_raw.get("risk_score", 0)
    final_confidence    = ai_raw.get("confidence", 0.7)
    final_narrative     = ai_raw.get("narrative", "AI evaluation.")
    # NEW: Extract Chain of Thought for detailed auditing
    chain_of_thought_list = ai_raw.get("chain_of_thought", [])
    if isinstance(chain_of_thought_list, list):
        # Join list into a readable paragraph for the DB text column
        llm_reasoning_str = "\n".join(chain_of_thought_list)
    else:
        llm_reasoning_str = str(chain_of_thought_list)

    # 5) Log AI decision into rt.risk_withdraw_decision
    ai_source = "AI_AGENT_REVIEW"
    result    = {
        "decision": final_decision,
        "primary_threat": final_primary_threat,
        "risk_score": final_risk_score,
        "confidence": final_confidence,
        "narrative": final_narrative,
        "llm_reasoning": llm_reasoning_str  # Full audit trail
    }

    core.log_decision_to_db(
        user_code,
        txn_id,
        result,
        features,
        ai_source,
        processing_time_ms=ai_ms,
    )

    # 6) Send Lark notification for HOLD/REJECT (final decision)
    #reserved for future lark enablement
    final_payload = {
        "user_code": user_code,
        "txn_id": txn_id,
        "decision": final_decision,
        "reasons": [final_narrative],
        "risk_score": final_risk_score,
        "primary_threat": final_primary_threat,
        "source": ai_source,
        "withdrawal_amount": withdrawal_amount,
        "withdraw_currency": withdraw_currency,
        "processing_time_ms": ai_ms,
    }

    

    print(f"[AI_WORKER] Finished txn user_code={user_code}, txn_id={txn_id}, decision={final_decision}")
    return True


def handler(event, context):
    """
    Function Compute entrypoint for the async AI worker.

    Configure this FC with:
      - Trigger Type: Timer
      - Schedule: e.g. every 30 seconds or 1 minute
    """
    print("[AI_WORKER] Timer triggered. Starting batch.")

    total_processed = 0
    rows = _fetch_pending_hold_txns(limit=50)
    print(f"[AI_WORKER] Found {len(rows)} pending HOLD transactions for AI.")

    for user_code, txn_id in rows:
        try:
            if _process_single_txn(user_code, txn_id):
                total_processed += 1
        except Exception as e:
            print(f"[AI_WORKER] Error processing txn {user_code}/{txn_id}: {e}")

    result_msg = f"AI worker processed {total_processed} transactions."
    print("[AI_WORKER]", result_msg)

    return {
        "statusCode": 200,
        "body": json.dumps({"message": result_msg}),
        "isBase64Encoded": False,
    }
