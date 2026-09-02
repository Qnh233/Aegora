---json
{
  "name": "membership_info",
  "title": "会员权益介绍",
  "description": "用户明确咨询购买、升级、会员权益，或功能受限且会员可能提供帮助时，克制地补充会员介绍。",
  "product_id": "aicoin",
  "domain": "membership",
  "skill_type": "commercial",
  "source": "manual",
  "priority": 20,
  "trigger_rules": {
    "exclude_terms": ["投诉", "没到账", "退款", "爆仓", "亏损"],
    "strong_related_terms": ["功能受限", "额度不足"],
    "trigger_examples": ["我想购买会员", "会员有什么权益", "这个功能受限了"]
  },
  "metadata": {
    "owner": "customer-service"
  }
}
---
先直接回答用户当前问题。若用户明确询问购买、升级或权益，可以自然提示会员能够提供更多功能，并引导其查看最新官方会员介绍。不要编造价格、权益数量、优惠、有效期或购买链接；这些易变事实必须来自 FAQ 或业务工具。强相关但非明确购买场景中，只允许简短提示一次，不应反复营销。
