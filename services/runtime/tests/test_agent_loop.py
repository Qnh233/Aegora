from __future__ import annotations

import unittest
from threading import Barrier

from aegora_runtime.agent_loop import (
    AgentDecision,
    AgentDependencies,
    AgentRequest,
    SelfCheckResult,
    ToolCall,
    default_self_check,
    merge_ranked_results,
    run_agent,
    skill_observation_summary,
    static_dependencies,
)
from aegora_runtime.config import load_settings
from aegora_runtime.tools import ToolMetadata, RuntimeToolExecutor, ToolSpec, ToolValidationError


class AgentLoopTest(unittest.TestCase):
    def test_agent_request_derives_user_id_from_session(self) -> None:
        request = AgentRequest(query="q", session_id="external-session", user_id="ignored-user")

        self.assertEqual(request.session_id, "external-session")
        self.assertEqual(request.user_id, "ignored-user")

    def test_agent_request_falls_back_to_session_owner_when_user_id_missing(self) -> None:
        request = AgentRequest(query="q", session_id="external-session")

        self.assertEqual(request.session_id, "external-session")
        self.assertEqual(request.user_id, "session-owner:external-session")

    def test_skill_observation_summary_truncates_content(self) -> None:
        summary = skill_observation_summary({"content": "字" * 200}, content_limit=20)

        self.assertEqual(summary["content_preview"], "字" * 20 + "...")

    def test_direct_faq_answer_planner_flow(self) -> None:
        result = run_agent(
            AgentRequest(query="如何设置悬浮窗"),
            static_dependencies("安卓、iOS 以及电脑端均支持悬浮窗设置。"),
            load_settings(env_path=None),
            enable_pocoflow_db=False,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["route"], "faq_answer")
        self.assertIn("悬浮窗", result["answer"])
        self.assertGreaterEqual(len(result["observability"]), 5)
        self.assertEqual(
            [step["node"] for step in result["trace"]],
            [
                "LoadContextNode",
                "LoadSkillsNode",
                "PreGuardNode",
                "ThinkNode",
                "SelfCheckNode",
            ],
        )
        self.assertEqual(result["loop_mode"], "planner")
        self.assertTrue(result["trace_id"])
        self.assertEqual(result["run_context"]["trace_id"], result["trace_id"])
        self.assertFalse(result["model_thinking_enabled"])

    def test_planner_mode_skips_classify_and_hybrid_search(self) -> None:
        calls = {"think": 0, "tool": 0}
        stream_events = []

        def think(state):
            calls["think"] += 1
            if calls["think"] == 1:
                return AgentDecision(route="tool_call", tool_name="search_faq", tool_args={"query": "如何设置悬浮窗"})
            return AgentDecision(route="faq_answer", answer="手机端支持悬浮窗。")

        def run_tool(name, args, state):
            calls["tool"] += 1
            return {
                "tool_name": name,
                "args": args,
                "status": "ok",
                "output": {"results": [{"faq_id": 34, "title": "悬浮窗", "response": "手机端支持悬浮窗。"}]},
            }

        deps = AgentDependencies(
            load_context=lambda request: {},
            think=think,
            run_tool=run_tool,
            self_check=default_self_check,
            pre_guard=lambda request, context: {"route_hint": "planner", "reason": "planner_allowed"},
        )

        result = run_agent(
            AgentRequest(query="如何设置悬浮窗", stream_handler=stream_events.append),
            deps,
            load_settings(env_path=None),
            loop_mode="planner",
            enable_pocoflow_db=False,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["route"], "faq_answer")
        self.assertEqual(result["retrieved_faqs"][0]["faq_id"], 34)
        self.assertEqual(calls, {"think": 2, "tool": 1})
        self.assertTrue(any(item["type"] == "agent_decision" for item in stream_events))
        self.assertTrue(any(item["type"] == "tool_call" for item in stream_events))
        self.assertTrue(any("工具返回" in item["message"] for item in stream_events))
        self.assertEqual(
            [step["node"] for step in result["trace"]],
            [
                "LoadContextNode",
                "LoadSkillsNode",
                "PreGuardNode",
                "ThinkNode",
                "ExecuteToolNode",
                "ThinkNode",
                "SelfCheckNode",
            ],
        )

    def test_model_usage_is_reported_as_per_run_delta(self) -> None:
        usage = {"total_calls": 10, "failed_calls": 1, "by_model": {"m": {"calls": 10, "failed_calls": 1, "prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}}}

        def think(state):
            usage["total_calls"] += 1
            usage["by_model"]["m"]["calls"] += 1
            usage["by_model"]["m"]["prompt_tokens"] += 3
            usage["by_model"]["m"]["completion_tokens"] += 2
            usage["by_model"]["m"]["total_tokens"] += 5
            return AgentDecision(route="faq_answer", answer="ok")

        deps = AgentDependencies(
            load_context=lambda request: {},
            think=think,
            run_tool=lambda name, args, state: {},
            self_check=default_self_check,
            model_usage=lambda: {
                "total_calls": usage["total_calls"],
                "failed_calls": usage["failed_calls"],
                "by_model": {"m": dict(usage["by_model"]["m"])},
            },
        )

        result = run_agent(AgentRequest(query="q"), deps, load_settings(env_path=None), enable_pocoflow_db=False)

        self.assertEqual(result["model_usage"]["total_calls"], 1)
        self.assertEqual(result["model_usage"]["by_model"]["m"]["total_tokens"], 5)

    def test_node_end_observability_records_usage_delta(self) -> None:
        usage = {"total_calls": 0, "failed_calls": 0, "by_model": {"m": {"calls": 0, "failed_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}}

        def think(state):
            usage["total_calls"] += 1
            usage["by_model"]["m"]["calls"] += 1
            usage["by_model"]["m"]["prompt_tokens"] += 4
            usage["by_model"]["m"]["completion_tokens"] += 3
            usage["by_model"]["m"]["total_tokens"] += 7
            return AgentDecision(route="faq_answer", answer="ok")

        deps = AgentDependencies(
            load_context=lambda request: {},
            think=think,
            run_tool=lambda name, args, state: {},
            self_check=default_self_check,
            model_usage=lambda: {
                "total_calls": usage["total_calls"],
                "failed_calls": usage["failed_calls"],
                "by_model": {"m": dict(usage["by_model"]["m"])},
            },
        )

        result = run_agent(AgentRequest(query="q"), deps, load_settings(env_path=None), enable_pocoflow_db=False)
        think_end = next(
            item
            for item in result["observability"]
            if item.get("event") == "node_end" and item.get("node") == "ThinkNode"
        )
        flow_end = next(item for item in result["observability"] if item.get("event") == "flow_end")

        self.assertEqual(think_end["model_usage"]["by_model"]["m"]["total_tokens"], 7)
        self.assertEqual(flow_end["model_usage"]["by_model"]["m"]["total_tokens"], 7)

    def test_planner_observability_records_pre_guard(self) -> None:
        deps = AgentDependencies(
            load_context=lambda request: {"history": [{"role": "user", "content": "如何设置悬浮窗？"}]},
            think=lambda state: AgentDecision(route="faq_answer", answer="手机端支持悬浮窗。"),
            run_tool=lambda name, args, state: {},
            self_check=default_self_check,
            pre_guard=lambda request, context: {"route_hint": "planner", "reason": "followup_allowed"},
        )

        result = run_agent(AgentRequest(query="那手机端呢？"), deps, load_settings(env_path=None), enable_pocoflow_db=False)

        pre_guard_event = next(item for item in result["observability"] if item.get("event") == "pre_guard")
        self.assertEqual(pre_guard_event["query"], "那手机端呢？")
        self.assertEqual(pre_guard_event["history_count"], 1)
        self.assertEqual(pre_guard_event["reason"], "followup_allowed")

    def test_load_skills_injects_context_before_think(self) -> None:
        think_context = {}

        def think(state):
            think_context.update(state["context"])
            return AgentDecision(route="chat", answer=state["skills"][0]["content"])

        deps = AgentDependencies(
            load_context=lambda request: {"history": []},
            load_skills=lambda request, context: {
                "query": request.query,
                "candidates": [{"id": 1, "decision": "eligible"}],
                "skills": [{"id": 1, "name": "membership_info", "content": "介绍会员权益"}],
                "skipped": False,
                "reason": "retrieved",
            },
            think=think,
            run_tool=lambda name, args, state: {},
            self_check=default_self_check,
        )

        result = run_agent(AgentRequest(query="想了解会员"), deps, load_settings(env_path=None), enable_pocoflow_db=False)

        assert result["answer"] == "介绍会员权益"
        assert think_context["skills"][0]["name"] == "membership_info"
        event = next(item for item in result["observability"] if item.get("event") == "skill_retrieval")
        assert event["injected_skill_ids"] == [1]
        assert isinstance(event["elapsed_ms"], float)
        assert event["loaded_skills"] == [
            {
                "id": 1,
                "name": "membership_info",
                "title": None,
                "skill_type": None,
                "scope": None,
                "availability": None,
                "retrieval_score": None,
                "injection_reason": None,
                "content_preview": "介绍会员权益",
            }
        ]

    def test_merge_ranked_results_keeps_top_results_for_each_independent_question(self) -> None:
        merged = merge_ranked_results(
            ["如何设置预警？", "如何取消网格图？"],
            [
                [{"faq_id": 1, "title": "预警"}, {"faq_id": 3, "title": "次要预警"}],
                [{"faq_id": 2, "title": "网格图"}, {"faq_id": 3, "title": "次要预警"}],
            ],
        )

        self.assertEqual([item["faq_id"] for item in merged], [1, 2, 3])
        self.assertEqual(merged[2]["matched_queries"], ["如何设置预警？", "如何取消网格图？"])

    def test_tool_call_loops_back_to_think(self) -> None:
        calls = {"think": 0, "tool": 0}

        def think(state):
            calls["think"] += 1
            if calls["think"] == 1:
                return AgentDecision(route="tool_call", tool_name="lookup_order", tool_args={"id": "1"})
            return AgentDecision(route="faq_answer", answer="查询完成，需要按页面提示继续操作。")

        def run_tool(name, args, state):
            calls["tool"] += 1
            return {"name": name, "args": args, "status": "ok"}

        deps = AgentDependencies(
            load_context=lambda request: {},
            think=think,
            run_tool=run_tool,
            self_check=default_self_check,
        )

        result = run_agent(AgentRequest(query="帮我查订单"), deps, load_settings(env_path=None), enable_pocoflow_db=False)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["loop_count"], 2)
        self.assertEqual(calls, {"think": 2, "tool": 1})
        self.assertEqual(result["tool_observations"][0]["name"], "lookup_order")
        tool_events = [item for item in result["observability"] if item.get("event") == "tool_call"]
        self.assertEqual(tool_events[0]["tool_name"], "lookup_order")
        self.assertEqual(tool_events[0]["reason"], None)
        self.assertEqual(tool_events[0]["output"], {"name": "lookup_order", "args": {"id": "1"}, "status": "ok"})

    def test_runtime_tool_executor_lifecycle_hooks(self) -> None:
        events = []
        executor = RuntimeToolExecutor()
        executor.hooks.before_call.append(events.append)
        executor.hooks.after_call.append(events.append)
        executor.register(
            ToolSpec(
                name="lookup_order",
                description="lookup order by id",
                input_schema={"id": str},
                handler=lambda args, state: {"order_id": args["id"], "state": "paid"},
            )
        )

        output = executor.run("lookup_order", {"id": "A1"}, {})

        self.assertEqual(output["status"], "ok")
        self.assertEqual(output["output"]["state"], "paid")
        self.assertEqual([event["event"] for event in events], ["tool_before", "tool_after"])

    def test_read_only_parallel_safe_tools_run_in_parallel(self) -> None:
        barrier = Barrier(2, timeout=1)
        executor = RuntimeToolExecutor()
        executor.register(
            ToolSpec(
                name="read_value",
                description="read",
                input_schema={"key": str},
                handler=lambda args, state: {"key": args["key"], "waited": barrier.wait()},
                metadata=ToolMetadata(read_only=True, parallel_safe=True, idempotent=True),
            )
        )

        output = executor.run_many(
            [
                {"call_id": "c1", "tool_name": "read_value", "tool_args": {"key": "a"}},
                {"call_id": "c2", "tool_name": "read_value", "tool_args": {"key": "b"}},
            ],
            {},
        )

        self.assertEqual(output["execution_mode"], "parallel")
        self.assertEqual([item["call_id"] for item in output["results"]], ["c1", "c2"])
        self.assertTrue(all(item["tool_metadata"]["read_only"] for item in output["results"]))

    def test_load_skill_tool_result_becomes_available_in_same_loop(self) -> None:
        calls = {"think": 0}

        def think(state):
            calls["think"] += 1
            if calls["think"] == 1:
                return AgentDecision(
                    route="tool_call",
                    tool_name="runtime.load_skill",
                    tool_args={"name": "exchange_auth", "reason": "需要授权经验"},
                )
            return AgentDecision(route="faq_answer", answer=state["skills"][0]["content"])

        deps = AgentDependencies(
            load_context=lambda request: {},
            load_skills=lambda request, context: {
                "query": request.query,
                "skills": [],
                "auto_skills": [],
                "session_skills": [],
                "skill_index": [{"name": "exchange_auth"}],
            },
            think=think,
            run_tool=lambda name, args, state: {
                "tool_name": name,
                "args": args,
                "status": "ok",
                "output": {
                    "loaded": True,
                    "already_loaded": False,
                    "skill": {"id": 6, "name": "exchange_auth", "content": "先识别授权平台"},
                },
            },
            self_check=default_self_check,
        )

        result = run_agent(
            AgentRequest(query="币安授权"),
            deps,
            load_settings(env_path=None),
            loop_mode="planner",
            enable_pocoflow_db=False,
        )

        self.assertEqual(result["answer"], "先识别授权平台")
        self.assertEqual(result["skills"][0]["availability"], "tool_loaded")

    def test_batch_with_write_tool_runs_sequentially(self) -> None:
        order = []
        executor = RuntimeToolExecutor()
        executor.register(
            ToolSpec(
                name="read_value",
                description="read",
                input_schema={"key": str},
                handler=lambda args, state: order.append(f"read:{args['key']}") or {},
                metadata=ToolMetadata(read_only=True, parallel_safe=True, idempotent=True),
            )
        )
        executor.register(
            ToolSpec(
                name="save_value",
                description="write",
                input_schema={"key": str},
                handler=lambda args, state: order.append(f"write:{args['key']}") or {},
                metadata=ToolMetadata(side_effects="memory_write"),
            )
        )

        output = executor.run_many(
            [
                {"call_id": "c1", "tool_name": "read_value", "tool_args": {"key": "a"}},
                {"call_id": "c2", "tool_name": "save_value", "tool_args": {"key": "b"}},
            ],
            {},
        )

        self.assertEqual(output["execution_mode"], "sequential")
        self.assertEqual(order, ["read:a", "write:b"])

    def test_agent_executes_multiple_tool_calls_in_one_loop(self) -> None:
        calls = {"think": 0}

        def think(state):
            calls["think"] += 1
            if calls["think"] == 1:
                return AgentDecision(
                    route="tool_call",
                    tool_calls=(
                        ToolCall(call_id="faq-1", tool_name="support.search_faq", tool_args={"query": "预警"}),
                        ToolCall(call_id="faq-2", tool_name="support.search_faq", tool_args={"query": "网格图"}),
                    ),
                )
            return AgentDecision(route="faq_answer", answer="已分别回答。")

        def run_tools(tool_calls, state):
            return {
                "execution_mode": "parallel",
                "results": [
                    {
                        "call_id": call["call_id"],
                        "tool_name": call["tool_name"],
                        "args": call["tool_args"],
                        "status": "ok",
                        "output": {"query": call["tool_args"]["query"], "results": [{"faq_id": index + 1}]},
                    }
                    for index, call in enumerate(tool_calls)
                ],
            }

        deps = AgentDependencies(
            load_context=lambda request: {},
            think=think,
            run_tool=lambda name, args, state: {},
            run_tools=run_tools,
            self_check=default_self_check,
        )

        result = run_agent(
            AgentRequest(query="预警怎么设置，网格图怎么取消"),
            deps,
            load_settings(env_path=None),
            loop_mode="planner",
            enable_pocoflow_db=False,
        )

        self.assertEqual(result["loop_count"], 2)
        self.assertEqual(len(result["tool_observations"]), 2)
        self.assertEqual([item["faq_id"] for item in result["retrieved_faqs"]], [1, 2])
        batch_event = next(item for item in result["observability"] if item.get("event") == "tool_batch")
        self.assertEqual(batch_event["execution_mode"], "parallel")

    def test_runtime_tool_executor_validates_args(self) -> None:
        executor = RuntimeToolExecutor()
        executor.register(
            ToolSpec(
                name="lookup_order",
                description="lookup order by id",
                input_schema={"id": str},
                handler=lambda args, state: {},
            )
        )

        with self.assertRaises(ToolValidationError):
            executor.run("lookup_order", {"id": 123}, {})

    def test_tool_error_goes_to_self_check(self) -> None:
        def think(state):
            if not state["tool_observations"]:
                return AgentDecision(route="tool_call", tool_name="broken_tool", tool_args={"id": "1"})
            return AgentDecision(route="handoff", answer="工具执行失败，需要人工处理。")

        deps = AgentDependencies(
            load_context=lambda request: {},
            think=think,
            run_tool=lambda name, args, state: (_ for _ in ()).throw(RuntimeError("boom")),
            self_check=default_self_check,
        )

        result = run_agent(AgentRequest(query="查订单"), deps, load_settings(env_path=None), enable_pocoflow_db=False)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tool_observations"][0]["status"], "error")
        self.assertEqual(result["route"], "handoff")

    def test_clarify_route_completes_without_tool(self) -> None:
        deps = AgentDependencies(
            load_context=lambda request: {},
            think=lambda state: AgentDecision(route="clarify", answer="请补充您要下载的是 APP 还是 PC 端？"),
            run_tool=lambda name, args, state: {},
            self_check=default_self_check,
        )

        result = run_agent(AgentRequest(query="下载"), deps, load_settings(env_path=None), enable_pocoflow_db=False)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["route"], "clarify")
        self.assertIn("APP", result["answer"])

    def test_self_check_failure_marks_failed(self) -> None:
        deps = AgentDependencies(
            load_context=lambda request: {},
            think=lambda state: AgentDecision(route="faq_answer", answer="无依据答案"),
            run_tool=lambda name, args, state: {},
            self_check=lambda state: SelfCheckResult(passed=False, reason="unsupported_answer"),
        )

        result = run_agent(AgentRequest(query="不存在的问题"), deps, load_settings(env_path=None), enable_pocoflow_db=False)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"], ["unsupported_answer"])


if __name__ == "__main__":
    unittest.main()
