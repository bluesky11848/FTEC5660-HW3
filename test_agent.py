"""Offline checks: python -m unittest -v test_agent (no paid API calls)."""
import asyncio
import unittest
from unittest.mock import patch

import hw3


VALUES = {
    "revenue_2023": 1000, "revenue_2025": 1300,
    "costs_2023": 900, "costs_2025": 1144,
    "growth_pct": 30, "industry_growth_pct": 20,
    "margin_2023_pct": 10, "margin_2025_pct": 12,
    "industry_margin_pct": 11, "margin_change_pp": 2,
    "pass_1": 1, "pass_5": 1, "pass_6": 1,
    "sales_months_2023": 12, "sales_months_2025": 12,
    "cost_months_2023": 12, "cost_months_2025": 12,
}


def table(**updates):
    data = {**VALUES, **updates}
    return " | ".join(data) + "\n" + " | ".join(map(str, data.values()))


class FakeTool:
    name = "sql_query"

    def __init__(self, output):
        self.output = output
        self.calls = 0

    async def ainvoke(self, args):
        self.calls += 1
        assert args == {"query": hw3.EVIDENCE_SQL}
        return self.output


class FakeChain:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = 0

    async def ainvoke(self, args):
        self.calls += 1
        assert "evidence" in args
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


GOOD_AUDIT = {
    "points": {"1": "PASS", "5": "PASS", "6": "PASS"},
    "interpretation": "Growth and profitability meet the benchmarks while margins improve.",
}


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_evidence_is_rejected(self):
        for raw in ["SQL error: missing table", "a\n(no rows)",
                    table(revenue_2023=0), table(margin_change_pp="nan"),
                    table(cost_months_2025=6), table(pass_1=2)]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                hw3.parse_evidence(raw)

    async def test_full_precision_flags_override_display_rounding(self):
        # Both displayed margins are 10.00, but SQL saw a slight decrease.
        tool = FakeTool(table(margin_2023_pct=10, margin_2025_pct=10,
                              margin_change_pp=0, pass_6=0))
        result = await hw3.analyze_companies(
            {"chain": None, "status": "test"}, {"unseen_name": [tool]})
        self.assertEqual(result["unseen_name"]["points"][6], "FAIL")
        self.assertEqual(tool.calls, 1)

    async def test_bad_company_does_not_crash_good_company(self):
        result = await hw3.analyze_companies(
            {"chain": None, "status": "test"},
            {"broken": [FakeTool("SQL error: missing table")],
             "healthy": [FakeTool(table())]})
        self.assertEqual(result["broken"]["points"], {})
        self.assertEqual(result["healthy"]["points"], {1: "PASS", 5: "PASS", 6: "PASS"})

    async def test_model_verdict_conflict_gets_one_repair(self):
        wrong = {**GOOD_AUDIT, "points": {"1": "FAIL", "5": "PASS", "6": "PASS"}}
        chain = FakeChain([wrong, GOOD_AUDIT])
        result = await hw3.analyze_companies({"chain": chain}, {"sample": [FakeTool(table())]})
        self.assertEqual(chain.calls, 2)
        self.assertIn(GOOD_AUDIT["interpretation"], result["sample"]["summary"])
        self.assertNotIn("fallback", result["sample"]["summary"])

    async def test_api_failure_preserves_tool_verdicts_without_leaking_error(self):
        chain = FakeChain([RuntimeError("private-provider-detail"), RuntimeError("secret")])
        result = await hw3.analyze_companies({"chain": chain}, {"sample": [FakeTool(table())]})
        self.assertEqual(chain.calls, 2)
        self.assertEqual(result["sample"]["points"], {1: "PASS", 5: "PASS", 6: "PASS"})
        self.assertIn("fallback", result["sample"]["summary"])
        self.assertNotIn("private-provider-detail", result["sample"]["summary"])
        self.assertNotIn("secret", result["sample"]["summary"])

    async def test_invalid_model_output_is_not_published(self):
        invalid = {**GOOD_AUDIT, "interpretation": "Revenue will grow 500%."}
        chain = FakeChain([[], invalid])
        result = await hw3.analyze_companies({"chain": chain}, {"sample": [FakeTool(table())]})
        self.assertIn("fallback", result["sample"]["summary"])
        self.assertNotIn("500", result["sample"]["summary"])

    async def test_real_langchain_prompt_and_json_parser_with_mock_model(self):
        import json
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda

        def mock_model(prompt):
            messages = prompt.to_messages()
            self.assertIn("SQL evidence", messages[-1].content)
            self.assertIn("pass_6", messages[-1].content)
            return AIMessage(content=json.dumps(GOOD_AUDIT))

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-placeholder"}), \
             patch("langchain_deepseek.ChatDeepSeek", return_value=RunnableLambda(mock_model)) as factory:
            agent = hw3.build_agent()
            self.assertEqual(factory.call_args.kwargs["model"], "deepseek-v4-flash")
            result = await hw3.analyze_companies(agent, {"sample": [FakeTool(table())]})
            self.assertIn(GOOD_AUDIT["interpretation"], result["sample"]["summary"])

    async def test_tool_selection_is_by_name(self):
        class UnusedTool:
            name = "interview"

        result = await hw3.analyze_companies(
            {"chain": None, "status": "test"},
            {"sample": [UnusedTool(), FakeTool(table())]})
        self.assertEqual(len(result["sample"]["points"]), 3)

    async def test_no_api_key_is_explicitly_reported(self):
        with patch.dict("os.environ", {}, clear=True):
            agent = hw3.build_agent()
        self.assertIsNone(agent["chain"])
        result = await hw3.analyze_companies(agent, {"sample": [FakeTool(table())]})
        self.assertIn("model audit not run", result["sample"]["summary"])


if __name__ == "__main__":
    unittest.main()
