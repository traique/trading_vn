"""The rating heuristic that reads the decision's system action.

The Portfolio Manager's rendered decision always carries a ``**Rating**: X``
header with the quant engine's action, so the rating is read deterministically;
no second model call is made.
"""

import pytest

from tradingagents.agents.rating import RATING_REVIEW, RATINGS_5_TIER, extract_rating, parse_rating

# ---------------------------------------------------------------------------
# Heuristic parser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseRating:
    def test_explicit_label_buy(self):
        assert parse_rating("Rating: Buy\nReasoning here.") == "BUY"

    def test_explicit_label_watch(self):
        assert parse_rating("Rating: Watch\nDetails.") == "WATCH"

    def test_explicit_label_vietnamese(self):
        assert parse_rating("Rating: Mua\nChi tiết.") == "BUY"
        assert parse_rating("Rating: Bán\nChi tiết.") == "SELL"

    def test_retired_tier_is_not_a_rating(self):
        # Overweight/Underweight are not on the engine's scale.
        assert parse_rating("Rating: Overweight\nDetails.") == RATING_REVIEW

    def test_explicit_label_with_markdown_bold_value(self):
        # Regression: Rating: **Sell** — markdown around the value.
        assert parse_rating("Rating: **Sell**\nExit immediately.") == "SELL"

    def test_explicit_label_with_markdown_bold_label(self):
        assert parse_rating("**Rating**: NO_TRADE\nStay out.") == "NO_TRADE"

    def test_rendered_pm_markdown_shape(self):
        # The exact shape produced by render_pm_decision must always parse.
        from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision

        text = render_pm_decision(
            PortfolioDecision(
                executive_summary="Mua vùng 70.500-71.200 VND, tỷ trọng 6% NAV.",
                investment_thesis="Lợi nhuận quý tăng; khối ngoại mua ròng.",
                key_risks="Thủng 67.000 VND.",
            ),
            {"action": "BUY", "target_price": 78000},
        )
        assert parse_rating(text) == "BUY"

    def test_explicit_label_wins_over_prose_with_markdown(self):
        text = (
            "The buy thesis is weakened by guidance.\n"
            "Rating: **Sell**\n"
            "Exit before earnings."
        )
        assert parse_rating(text) == "SELL"

    def test_no_rating_is_flagged_for_review_not_defaulted(self):
        # A decision nobody can read is not a Hold; recording one invents a call.
        assert parse_rating("No clear directional signal at this time.") == RATING_REVIEW

    def test_no_rating_custom_default(self):
        assert parse_rating("Plain prose.", default="NO_TRADE") == "NO_TRADE"

    def test_all_five_tiers_recognised(self):
        assert set(RATINGS_5_TIER) == {"BUY", "HOLD", "WATCH", "NO_TRADE", "SELL"}
        for r in RATINGS_5_TIER:
            assert parse_rating(f"Rating: {r}") == r
            assert parse_rating(f"Rating: {r.lower()}") == r

    def test_fullwidth_colon_is_parsed_not_reviewed(self):
        # `Rating：Watch` (fullwidth colon) is read, not sent to review (#1170).
        assert parse_rating("Rating：Watch\n理由はこちら。") == "WATCH"


@pytest.mark.unit
class TestExtractRating:
    def test_returns_none_when_absent(self):
        assert extract_rating("No directional call here.") is None
        assert extract_rating("") is None

    def test_whole_word_only(self):
        # substrings inside larger words must not match
        assert extract_rating("The buyer was holding shares.") is None

    def test_parse_rating_defaults_to_review(self):
        # The memory log tags an unreadable decision REVIEW, never a tradeable rating.
        assert parse_rating("No rating here.") == RATING_REVIEW
        assert parse_rating("No rating here.", default="NO_TRADE") == "NO_TRADE"
