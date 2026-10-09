# 基线结论（真实排查沉淀）

来源：2026-09-18 的一次真实只读排查，环境为 Codex CLI `0.155.0-alpha.9` +
本地 cc-switch 3.20.3 + DeepSeek 官方 API。数字是那次环境的快照，不要当常量套用；
用 `scripts/analyze_tokens.py` 在当前环境重新取一遍。

## 链路结构

```text
Codex  --HTTP-->  本地 cc-switch 127.0.0.1:15721/v1  --SSE-->  https://api.deepseek.com/v1/responses
```

- `wire_api = "responses"`，streaming 开启，`model_reasoning_effort = "high"`。
- provider 名为 `custom`（显示 deepseek）。敏感信息（bearer / api key）永远打码。

## 上下文与阈值

| 项 | 值 | 含义 |
|---|---|---|
| context_window | 1,048,576 | 模型 catalog 标称窗口 |
| effective_context_window_percent | 95 | 硬上限按窗口的 95% 算 |
| full_context_window_limit | 996,147 | 运行期硬上限（= 1,048,576 × 95%） |
| auto_compact_scope_limit | 943,718 | 自动 compaction 触发基准（≈ 90%） |
| auto_compact_limit_scope | Total | 基准按线程总用量计算 |

脚本的阈值来自 `~/.codex/logs_2.sqlite` 的 `logs.feedback_log_body`，形如：

```text
... total_usage_tokens=89319 auto_compact_scope_tokens=89319
auto_compact_scope_limit=Some(943718) auto_compact_limit_scope=Total
auto_compact_window_prefill_tokens=None full_context_window_limit=Some(996147)
full_context_window_limit_reached=false
```

- 如果 `config.toml` 里没显式设 `model_auto_compact_token_limit`，就用上面这套默认。
- `/compact` 可手动触发；`truncation_policy = { mode = "bytes", limit = 10000 }`
  是**单项/工具结果级别**的字节截断，不等于历史消息裁剪。
- DeepSeek Responses API 是无状态的：每次请求都重发完整历史，所以"累计 input token"会很大，
  命中缓存只降低计费成本，不消除逻辑上的重复 input。

## 缓存 miss 归因

对相邻两次请求：

```text
predicted_new = (input_N - input_{N-1}) - output_{N-1}
actual_miss   = input_N - cached_N
delta         = actual_miss - predicted_new
```

- `delta` 很小（实测中位数几十 token）→ miss = 上一轮输出之后新增的 `function_call_output`
  （shell / PowerShell / 数据库 / JSON 原始输出），**前缀稳定**。
- `delta` 很大 → 稳定前缀很可能被改写，重点排查下面「绝对不要动」。

实测样例（命中率长期 96%–99.5%）：

| 时间(UTC) | input | cached | miss | output | 预测新增 | delta |
|---|---:|---:|---:|---:|---:|---:|
| 14:47:30 | 156,942 | 153,344 | 3,598 | 3,881 | 3,575 | 23 |
| 14:47:37 | 162,153 | 160,768 | 1,385 | 1,886 | 1,330 | 55 |

## 优化方向

1. **减少大块工具原始输出**（最有效）：别让一条命令一次性吐几千字符完整结果，
   改成过滤/截断/分页后再进上下文。
2. 控制单个工具结果体积（借助 `truncation_policy`，但不要靠它兜底）。
3. 必要时手动 `/compact` 或拆分会话——但这是"降总量"，不是"修前缀"。
4. `reasoning_effort` 从 `high` 降档可减少 reasoning token，但会影响质量，需用户确认。

## 绝对不要动（会破坏稳定前缀，制造 cache miss）

- 不要中途切 model / provider / `base_url`。
- 不要中途启停 plugins / skills / MCP server。
- 不要改 system / developer instructions、host skills、permissions 的内容或**顺序**。
- 不要在原线程里新增或修改 `AGENTS.md` / memory 后再继续。
- 不要频繁 `/compact` / fork / resume。
- 不要在同一个 turn 内改 cwd / workspace roots / timezone / current date。
- 不要重排、删除、改写早期的历史 item。

## 数据来源

| 数据 | 位置 | 用途 |
|---|---|---|
| 会话请求用量 | `~/.codex/sessions/**/rollout-*.jsonl` | input/cached/output/reasoning、hit rate |
| 模型/链路 | `~/.codex/config.toml` | model、provider、base_url、wire_api |
| 窗口配置 | `~/.codex/cc-switch-model-catalog.json` | context_window、effective %、truncation |
| compaction 阈值 | `~/.codex/logs_2.sqlite` | auto_compact / full_context 限制 |