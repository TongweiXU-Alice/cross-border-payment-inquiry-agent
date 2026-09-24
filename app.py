"""Streamlit review surface for the synthetic cross-border payment workflow."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

import streamlit as st

from agent import run_agent
from setup_db import build
from tools.db import get_db_path


DECISION_LABELS = {
    "ANSWER": "可回答",
    "CLARIFY": "需要补充信息",
    "PARTIAL_RESOLUTION": "部分解决",
    "ESCALATE": "升级人工",
}

SCENARIOS = {
    "状态一致 · FL260002": {
        "query": "客户问FL260002现在钱到哪里了。",
        "focus": "检查内部状态与模拟银行状态一致时，Agent只陈述事实，不承诺到账时间。",
    },
    "费用差额 · FL260013": {
        "query": "客户汇了10000 USD，FL260013只收到9950，认为平台扣了50手续费，请确认。",
        "focus": "验证已记录费用，只把10 USD归因到有证据的记录，保留40 USD未解释差额。",
    },
    "退款链路 · FL260003": {
        "query": "FL260003为什么被退回？退款现在在哪里？",
        "focus": "沿合成退款记录追踪关联出金，不把Processing写成已到账。",
    },
    "状态冲突 · FL260024": {
        "query": "FL260024内部仍显示Processing，但银行侧显示Completed，客户催问资金状态。",
        "focus": "并列显示两个来源的值，进入Operations Review，不静默选择一方。",
    },
    "多条件歧义 · 无FL号": {
        "query": "客户查9月20日Citi的一笔5000 USD出金，没有FL号。",
        "focus": "展示三笔候选交易，只要求补充系统实际支持的FL号。",
    },
    "审查 + GPI · FL260015": {
        "query": "请确认FL260015现在在哪里、为什么被审查、预计什么时候到账，并提供GPI。",
        "focus": "同时处理状态、审查、ETA和文档元数据，但不猜原因、不承诺ETA、不提供真实文件下载。",
    },
}


def _ensure_database() -> str | None:
    path = get_db_path()
    if path.exists():
        return None
    try:
        build()
    except Exception as exc:  # pragma: no cover - shown in the UI only
        return str(exc)
    return None


def _mode_label() -> str:
    if (
        os.getenv("OPENAI_PARSER_ENABLED") == "1"
        and os.getenv("OPENAI_API_KEY")
        and os.getenv("OPENAI_MODEL")
    ):
        return "Optional OpenAI structured parser"
    return "Local deterministic parser"


def _status_message(decision: str) -> str:
    label = DECISION_LABELS.get(decision, decision)
    if decision == "ANSWER":
        return f"{label} · 现有合成证据支持当前请求。"
    if decision == "CLARIFY":
        return f"{label} · 当前字段不足以安全地唯一定位或回答。"
    if decision == "PARTIAL_RESOLUTION":
        return f"{label} · 已列出可验证事实，并保留未解释部分。"
    return f"{label} · 需要人工跟进，系统不会选择冲突来源或补写原因。"


def _fact_rows(result: Dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for record in result.get("evidence", []):
        row = dict(record)
        if isinstance(row.get("value"), (dict, list)):
            row["value"] = json.dumps(row["value"], ensure_ascii=False)
        rows.append(row)
    return rows


def _unsupported_rows(result: Dict[str, Any]) -> list[dict[str, str]]:
    labels = {
        "estimated_arrival_time": "预计到账时间",
        "review_reason": "审查原因",
        "remaining_amount_difference_reason": "剩余金额差额原因",
        "refund_record_missing": "退款记录",
        "verified_fee_evidence_missing": "已验证费用记录",
        "received_amount_missing": "到账金额",
        "document_metadata_unavailable": "文档元数据",
        "GPI_metadata_unavailable": "GPI 元数据",
        "PAYMENT_PROOF_metadata_unavailable": "付款凭证元数据",
        "related_payout_not_settled": "退款后关联出金到账状态",
        "processing_over_synthetic_threshold": "合成处理时长阈值",
    }
    return [
        {"item": item, "说明": labels.get(item, "当前证据不足，不能安全回答")}
        for item in result.get("unsupported", [])
    ]


def _trace_rows(result: Dict[str, Any]) -> list[dict[str, Any]]:
    keep = ["step", "kind", "name", "purpose", "status", "source", "record_count", "summary", "duration_ms"]
    return [{key: event.get(key) for key in keep} for event in result.get("tool_trace", [])]


st.set_page_config(
    page_title="跨境支付智能问询 Agent",
    page_icon="💸",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .block-container { max-width: 1380px; padding-top: 2.2rem; padding-bottom: 2rem; }
    .hero { padding: 0 0 0.75rem 0; border-bottom: 1px solid #D7DFEA; margin-bottom: 1rem; }
    .hero h1 { color: #172033; margin-bottom: 0.25rem; letter-spacing: -0.02em; }
    .hero p { color: #536176; margin: 0; font-size: 1rem; }
    .small-note { color: #536176; font-size: 0.86rem; }
    [data-testid="stMetricValue"] { color: #172033; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="hero">
      <h1>跨境支付智能问询 Agent</h1>
      <p>Evidence-grounded workflow · local SQLite · draft only · synthetic data</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("演示范围")
    st.info("本页面只读取本地合成 SQLite 数据，不连接银行或支付网络，也不会发送外部消息。")
    st.caption(f"解析模式：{_mode_label()}")
    st.caption("数据快照：2026-09-22 12:00")
    st.caption("Human-in-the-loop 仅在当前浏览器会话中记录，不写回数据库。")
    st.divider()
    st.markdown("**可展示的能力**")
    st.markdown("交易定位 · 费用核验 · 退款链路 · 状态冲突 · 文档元数据")
    st.markdown("**不包含**")
    st.markdown("真实银行 / SWIFT-GPI / RAG / LangGraph / MCP / 自动发送")

db_error = _ensure_database()
if db_error:
    st.error(f"合成数据库初始化失败：{db_error}")
    st.stop()

scenario_name = st.selectbox("选择一个演示场景", list(SCENARIOS.keys()), index=3)
scenario = SCENARIOS[scenario_name]
st.caption(f"这个场景验证什么：{scenario['focus']}")

query = st.text_area("业务问询", value=scenario["query"], height=110, key="query_text")
run_clicked = st.button("运行 Agent", type="primary", use_container_width=False)

if run_clicked:
    if not query.strip():
        st.warning("请输入一条业务问询，或先选择演示场景。")
    else:
        with st.spinner("正在定位交易、读取合成工具并校验证据…"):
            result = run_agent(query.strip())
        st.session_state["last_result"] = result.to_dict()
        st.session_state["last_query"] = query.strip()
        st.session_state.pop("review_record", None)

result = st.session_state.get("last_result")
if not result:
    st.info("选择场景后点击“运行 Agent”。结果会按“结论 → 人工跟进 → 证据 → Tool Trace”展示。")
    st.stop()

decision = result.get("decision", "CLARIFY")
verified_count = len(result.get("evidence", []))
pending_count = len(result.get("unsupported", [])) + len(result.get("conflicts", []))
trace_count = len([event for event in result.get("tool_trace", []) if event.get("kind") == "data_access"])

if decision == "ANSWER":
    st.success(_status_message(decision))
elif decision == "CLARIFY":
    st.info(_status_message(decision))
elif decision == "PARTIAL_RESOLUTION":
    st.warning(_status_message(decision))
else:
    st.error(_status_message(decision))

metric_cols = st.columns(4)
metric_cols[0].metric("Decision", DECISION_LABELS.get(decision, decision))
metric_cols[1].metric("需人工跟进", "是" if result.get("human_review_required") else "否")
metric_cols[2].metric("已记录证据", verified_count)
metric_cols[3].metric("数据工具调用", trace_count)

left, right = st.columns([1.45, 1], gap="large")
with left:
    st.subheader("Draft Response")
    st.container(border=True).write(result.get("draft_response") or "未生成草稿。")
with right:
    st.subheader("Next Step")
    packet = result.get("review_packet") or {}
    if result.get("human_review_required"):
        st.warning("这是一份待人工复核的草稿。")
        queues = packet.get("target_queues") or result.get("escalation") or ["未指定队列"]
        st.write("建议队列：" + "、".join(queues))
        reasons = packet.get("reason_codes") or result.get("unsupported") or result.get("conflicts")
        if reasons:
            st.write("触发原因：" + "、".join(reasons))
    else:
        st.success("当前没有强制人工升级条件，但仍只生成草稿。")
    st.caption("不会发送或写回任何外部系统。")

    review_status = st.radio(
        "会话内复核状态",
        ["待复核", "已查看草稿", "需要补充证据"],
        key="review_status",
        horizontal=True,
    )
    review_note = st.text_input("复核备注（不会持久化）", key="review_note")
    if st.button("记录本次会话复核", key="save_review"):
        st.session_state["review_record"] = {
            "status": review_status,
            "note": review_note,
            "draft_only": True,
            "persisted": False,
        }
    if st.session_state.get("review_record"):
        st.caption("已记录：" + json.dumps(st.session_state["review_record"], ensure_ascii=False))

st.divider()
tabs = st.tabs(["已验证事实", "待验证/缺失", "冲突", "候选交易", "Tool Trace", "解析结果"])

with tabs[0]:
    evidence = _fact_rows(result)
    st.dataframe(evidence, hide_index=True, width="stretch") if evidence else st.info("暂无已验证事实。")

with tabs[1]:
    unsupported = _unsupported_rows(result)
    if unsupported:
        st.dataframe(unsupported, hide_index=True, width="stretch")
    else:
        st.success("当前没有未验证项。")

with tabs[2]:
    conflicts = result.get("conflicts", [])
    if conflicts:
        status_conflict = result.get("verified_facts", {}).get("status_conflict")
        if status_conflict:
            st.dataframe(
                [{"来源": "内部交易表", "值": status_conflict.get("internal_status")},
                 {"来源": "模拟银行状态表", "值": status_conflict.get("bank_status")}],
                hide_index=True,
                width="stretch",
            )
        st.write(conflicts)
    else:
        st.success("没有检测到来源冲突。")

with tabs[3]:
    candidates = result.get("matched_transactions", [])
    if candidates:
        st.dataframe(candidates, hide_index=True, width="stretch")
    else:
        st.info("本次没有候选交易列表。")

with tabs[4]:
    trace_rows = _trace_rows(result)
    st.dataframe(trace_rows, hide_index=True, width="stretch") if trace_rows else st.info("没有工具或校验步骤。")
    with st.expander("Evidence validation checks"):
        st.dataframe(result.get("evidence_checks", []), hide_index=True, width="stretch")
    with st.expander("Full synthetic run JSON"):
        st.json(result)

with tabs[5]:
    st.json(result.get("inquiry", {}))

st.markdown(
    '<p class="small-note">All records are synthetic. “Document available” means metadata only; the demo does not ship or download a real bank document.</p>',
    unsafe_allow_html=True,
)
