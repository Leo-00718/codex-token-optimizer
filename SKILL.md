---
name: codex-token-optimizer
description: "诊断 Codex 会话的 token 消耗与上下文缓存命中：算缓存命中率、把 cache miss 归因到「新增工具结果」还是「稳定前缀被改写」，并读出上下文窗口与自动 compaction 阈值。当用户问「token 怎么越用越多 / 消耗为什么这么高」「缓存命中率是多少 / 为什么有 cache miss」「长上下文怎么优化 / 要不要 compact」时使用。"
---

# Codex Token 消耗诊断

对一次 Codex 会话做**只读**诊断：token 从哪来、缓存命没命中、miss 是新增内容还是前缀被改写、以及该往哪优化。

## 何时用

- "这个会话 token 怎么涨这么快 / 越用越多"
- "缓存命中率多少 / 为什么有 cache miss"
- "长上下文怎么优化 / 要不要 compact"
- 需要判断"是不是稳定前缀被破坏了"

不适用：单纯查某个模型的 API 价格或计费规则。

## 核心方法

全部只读，四步：

1. **取数** —— 从会话 JSONL 取每一次请求的 `input_tokens / cached_input_tokens / output_tokens / reasoning_output_tokens`。
2. **算命中率** —— 最近 N 次 + 全局，用 `cached / input`。
3. **miss 归因** —— 对相邻两次请求：
   `预测新增 = (input_N − input_{N-1}) − output_{N-1}`，再对比实际 `uncached_N = input_N − cached_N`。
   差值很小 → miss 就是"上一轮输出之后新增的工具结果"，前缀稳定；差值很大 → 前缀可能被改写。
4. **读上限** —— 上下文窗口、有效上限（window × effective%）、自动 compaction 阈值。

## 跑诊断

```bash
python scripts/analyze_tokens.py                      # 默认：最近改动的会话
python scripts/analyze_tokens.py --session <会话ID片段或jsonl路径>
python scripts/analyze_tokens.py --recent 20 --json   # 调整窗口 / 输出 JSON
```

脚本只读，不写任何文件。输出：逐次请求表、命中率、miss 归因表、判定结论，以及窗口/阈值/触发来源。

## 怎么读结论

- `verdict: STABLE PREFIX` —— miss 基本等于新增工具结果（delta 通常几十 token）。
  优化方向是**减少大块工具原始输出**，不要动历史。
- `verdict: POSSIBLE PREFIX REWRITE` —— 列出 delta 异常大的请求，按 `references/baseline-findings.md`
  的「绝对不要动」清单逐条排查（中途切模型/启停 plugin/改 system 顺序等）。

两个常见误判：

- `eo-cache-status: MISS` 是边缘网关字段，**不是** DeepSeek Context Cache 指标，别用它下结论。
- `cached_input_tokens` 高不代表"没有重复计费"——DeepSeek Responses API 是无状态的，每次仍发送完整历史。

## 参考

- `references/baseline-findings.md` —— 一次真实排查的基线：链路结构、阈值含义、优化清单、
  「绝对不要动」清单。读结论前建议先看一眼，避免重复踩坑。