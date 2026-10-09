# codex-token-optimizer

一个 Codex **skill**：对一次 Codex 会话做**只读** token 消耗诊断。

回答三个问题：

1. token 从哪来？—— 每次请求的 input / cached / output / reasoning。
2. 缓存命没命中？—— 最近 N 次与全局的 cache hit rate。
3. cache miss 是"新增工具结果"还是"稳定前缀被改写"？—— 用相邻请求做归因。

顺带读出上下文窗口、有效上限与自动 compaction 阈值。

## 安装

把本目录放到 Codex 的 skills 目录下即可：

```text
~/.codex/skills/codex-token-optimizer/
```

## 用法

```bash
python scripts/analyze_tokens.py                       # 默认分析最近改动的会话
python scripts/analyze_tokens.py --session <会话ID片段或 jsonl 路径>
python scripts/analyze_tokens.py --recent 20 --json
```

脚本只读，不修改任何配置或会话数据。

## 归因方法

对相邻两次请求：

```text
predicted_new = (input_N - input_{N-1}) - output_{N-1}
actual_miss   = input_N - cached_N
delta         = actual_miss - predicted_new
```

`delta` 很小 → miss 就是上一轮输出之后新增的工具结果（前缀稳定）。
`delta` 很大 → 稳定前缀很可能被改写。

更多背景（链路、阈值、"绝对不要动"清单）见 `references/baseline-findings.md`。

## 许可

MIT