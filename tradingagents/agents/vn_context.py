"""Prompt blocks shared by every agent of the Vietnamese pipeline.

The quantitative engine (``tradingagents.quant.engine``) decides the action and
every number of the trade plan. These blocks hand that decision to the LLM
agents together with the grounding rules lananh-main enforces: the agents
explain, argue and stress-test; they never change the action or invent a price.
"""

from __future__ import annotations

from tradingagents.market import vn_rules

NO_INVENT_RULE = (
    "TUYỆT ĐỐI không tự bịa hoặc đổi bất kỳ con số giá/entry/stop/target/tỷ trọng nào - "
    "chỉ được dùng lại ĐÚNG các con số đã có trong dữ liệu được cấp. Nếu cần nhắc tới một mốc giá, "
    "phải là mốc đã có sẵn, không tự tính mốc mới."
)

GROUNDING_RULES = (
    "Quy tắc trình bày bắt buộc:\n"
    "- Mọi nhận định phải bám vào dữ liệu được cấp; phần nào thiếu thì nói thẳng \"chưa đủ dữ liệu\", "
    "gộp mọi phần thiếu vào ĐÚNG MỘT dòng ngắn ở cuối.\n"
    "- Mỗi mốc giá nhắc tới phải kèm khoảng cách % so với giá hiện tại đã được tính sẵn trong dữ liệu; "
    "dùng lại đúng con số đó, không tự tính lại.\n"
    "- Chỉ báo kỹ thuật được tính trên NẾN ĐÓNG CỬA phiên gần nhất, không phải giá đang khớp: khi mô tả "
    "phải gắn ngày của phiên đó.\n"
    "- Tin tức: chỉ tin đánh dấu ✅ mới là tin của chính mã; tin ⚠️ phải gọi rõ là tin ngành/thị trường "
    "chung. Mọi tin nhắc tới phải kèm ngày.\n"
    "- Không tự tính lãi/lỗ vị thế; nếu dữ liệu có dòng lãi/lỗ do hệ thống tính thì dùng lại đúng số đó.\n"
    "- Không hứa hẹn lợi nhuận. Số viết kiểu Việt Nam: dấu chấm phân cách nghìn, dấu phẩy thập phân "
    "(70.370 VND; -4,76%)."
)


def quant_decision(state) -> dict:
    """The engine's decision stored in graph state, or {} when it could not run."""
    return state.get("quant_decision") or {}


def market_rules(state) -> str:
    """The trading rules, narrowed to the stock's exchange when it is known."""
    exchange = quant_decision(state).get("exchange")
    if not exchange and state.get("company_of_interest"):
        from tradingagents.dataflows.symbols import exchange_of

        exchange = exchange_of(state["company_of_interest"])
    return vn_rules.rules_summary(exchange)


def quant_block(state, *, heading: str = "QUYẾT ĐỊNH CỦA HỆ THỐNG ĐỊNH LƯỢNG") -> str:
    """The engine report for a prompt, framed as fixed: no agent may change it."""
    report = state.get("quant_report") or ""
    if not report:
        return (
            f"[{heading}]\nHệ thống định lượng không chạy được cho lượt này (thiếu dữ liệu giá). "
            "Hành động mặc định: NO_TRADE (đứng ngoài). Không agent nào được tự đặt vùng mua, stop, "
            "target hay tỷ trọng."
        )
    return (
        f"[{heading} - action và mọi con số đã CHỐT bằng quy tắc định lượng; "
        "các agent chỉ được diễn giải, phản biện và chỉ ra rủi ro, KHÔNG được đổi]\n" + report
    )


def action_of(state) -> str:
    return quant_decision(state).get("action") or "NO_TRADE"
