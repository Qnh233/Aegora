# Aegora Runtime 评估集

这些评估集由 `scripts/generate_eval_sets.py` 从 `data/` 下的 Strapi 导出数据生成，不依赖第三方包。

## 文件

|文件|数量|用途|
|---|---:|---|
|`eval_core_100.jsonl`|100|MVP 核心回归门禁，覆盖 FAQ、模糊反问、转人工、安全输入|
|`eval_retrieval_sample_300.jsonl`|300|检索召回评估，主要计算 `Recall@5`、`MRR@5`、`Top1 Accuracy`|
|`eval_retrieval_holdout_300.jsonl`|300|不参与调参的知识库内泛化集，与 retrieval 300 按内容 hash 隔离|
|`eval_bad_feedback_100.jsonl`|100|失败/不满意场景回归，用于验证低置信、转人工和 Dream 改善|
|`eval_security_50.jsonl`|50|XSS、HTML、Prompt Injection 安全门禁|
|`eval_perf_seed_100.jsonl`|100|性能压测种子，不做语义打分，主要统计延迟、超时、降级|
|`manifest.json`|-|生成信息、数量和路由分布|

## Schema

每行是一个 JSON object：

```json
{
  "id": "faq_001",
  "query": "网格图可以取消吗",
  "history": [],
  "expected_route": "faq_answer",
  "expected_faq_ids": [97],
  "expected_category": "功能设置",
  "must_include": ["PC", "线设置"],
  "must_not_include": ["无法回答"],
  "source": "knowledge",
  "tags": ["faq", "retrieval", "功能设置"],
  "difficulty": "easy",
  "weight": 1.0
}
```

## 路由标签

|`expected_route`|含义|
|---|---|
|`faq_answer`|应基于知识库直接回答|
|`clarify`|信息不足，应追问，不应强答|
|`handoff`|账号、风控、资金、投诉等敏感场景，应转人工或给人工处理路径|
|`safe_reject_or_neutral_answer`|安全输入，应中性处理或拒绝危险请求|
|`not_scored`|性能种子，只统计系统指标|

## 重新生成

```bash
python3 scripts/generate_eval_sets.py
```

生成脚本固定随机种子 `20260608`，同一份 `data/` 输入会得到稳定输出。

生成检索 holdout：

```bash
PYTHONPATH=src conda run -n agentic-rag python scripts/generate_retrieval_holdout.py
```

Holdout 优先采用 FAQ 的备用问法或不同标题，但答案标签仍来自知识库，因此它是
“知识库内泛化集”，不等同于来自新用户流量的真正外部测试集。
