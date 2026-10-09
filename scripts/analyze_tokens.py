#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex token / context consumption diagnostic (read-only).

Reads a Codex session JSONL plus config.toml, the model catalog and the local
logs DB, then reports:

  * per-request input / cached / output / reasoning tokens
  * cache hit rate (recent window + overall)
  * cache-miss attribution between consecutive requests:
        predicted_new = (input_N - input_N-1) - output_N-1
        actual_miss   = input_N - cached_N
    A small gap means the miss is just new tool output appended after the
    previous turn completed (stable prefix). A large gap means the stable
    prefix was probably rewritten.
  * context window / effective limit / auto-compact thresholds

Read-only: never writes to config, sessions or the logs DB.
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import sys


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def resolve_session(spec):
    root = os.path.join(codex_home(), "sessions")
    if spec:
        if os.path.isfile(spec):
            return spec
        hits = [h for h in glob.glob(os.path.join(root, "**", "*" + spec + "*"), recursive=True)
                if os.path.isfile(h)]
        if not hits:
            sys.exit("session not found: " + spec)
        return max(hits, key=os.path.getmtime)
    cands = glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True)
    if not cands:
        sys.exit("no session files under " + root)
    return max(cands, key=os.path.getmtime)


def _usage_record(ts, turn, u):
    return {
        "ts": ts,
        "turn": turn,
        "input": u.get("input_tokens", 0) or 0,
        "cached": u.get("cached_input_tokens", 0) or 0,
        "output": u.get("output_tokens", 0) or 0,
        "reasoning": u.get("reasoning_output_tokens", 0) or 0,
        "total": u.get("total_tokens", 0) or 0,
    }


def load_usages(path):
    """Prefer token_usage_record items; fall back to token_count events."""
    primary, fallback, thread_id = [], [], None
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if "token_usage_record" not in line and "token_count" not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            p = obj.get("payload") or {}
            if obj.get("type") == "token_usage_record":
                thread_id = p.get("thread_id") or thread_id
                primary.append(_usage_record(obj.get("timestamp"), p.get("turn_id"), p.get("usage") or {}))
            elif obj.get("type") == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") or {}
                fallback.append(_usage_record(obj.get("timestamp"), None, info.get("last_token_usage") or {}))
    if not thread_id:
        m = re.search(r"(01[0-9a-f]{6}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", os.path.basename(path))
        thread_id = m.group(1) if m else None
    return (primary if primary else fallback), thread_id


def analyze(usages, recent):
    res = {"count": len(usages), "overall": None, "recent": None,
           "pairs": [], "verdict": {}}
    if not usages:
        return res
    tot_in = sum(r["input"] for r in usages)
    tot_cached = sum(r["cached"] for r in usages)
    res["overall"] = {
        "input": tot_in, "cached": tot_cached,
        "hit_rate": (tot_cached / tot_in) if tot_in else 0.0,
        "output": sum(r["output"] for r in usages),
        "reasoning": sum(r["reasoning"] for r in usages),
    }
    win = usages[-recent:] if len(usages) > recent else usages
    win_in = sum(r["input"] for r in win)
    win_cached = sum(r["cached"] for r in win)
    res["recent"] = {
        "n": len(win), "input": win_in, "cached": win_cached,
        "hit_rate": (win_cached / win_in) if win_in else 0.0,
        "avg_miss": ((win_in - win_cached) / len(win)) if win else 0.0,
    }
    pairs = []
    for a, b in zip(usages, usages[1:]):
        d_input = b["input"] - a["input"]
        predicted = d_input - a["output"]
        actual = b["input"] - b["cached"]
        pairs.append({"ts": b["ts"], "d_input": d_input, "input": b["input"], "cached": b["cached"],
                      "actual": actual, "prev_output": a["output"],
                      "predicted": predicted, "delta": actual - predicted})
    res["pairs"] = pairs
    if pairs:
        acts = sorted(abs(p["actual"]) for p in pairs)
        deltas = sorted(abs(p["delta"]) for p in pairs)
        med_act = acts[len(acts) // 2]
        med_delta = deltas[len(deltas) // 2]
        thr = max(500.0, med_act * 0.5)
        res["verdict"] = {
            "median_abs_actual_miss": med_act,
            "median_abs_delta": med_delta,
            "stable_prefix": med_delta <= max(50.0, med_act * 0.25),
            "prefix_break_candidates": [p for p in pairs if abs(p["delta"]) > thr],
        }
    return res


def read_config():
    home = codex_home()
    path = os.path.join(home, "config.toml")
    out = {"path": path, "exists": os.path.isfile(path)}
    if not out["exists"]:
        return out
    data = {}
    try:
        import tomllib
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        txt = open(path, "r", encoding="utf-8", errors="ignore").read()
        for key in ("model_provider", "model", "model_reasoning_effort", "model_catalog_json"):
            m = re.search(r"^\s*" + key + r"\s*=\s*\"([^\"]+)\"", txt, re.M)
            if m:
                data[key] = m.group(1)
    provider = data.get("model_provider")
    prov = ((data.get("model_providers") or {}).get(provider) or {}) if provider else {}
    out.update({
        "model": data.get("model"),
        "model_provider": provider,
        "provider_name": prov.get("name"),
        "base_url": prov.get("base_url"),
        "wire_api": prov.get("wire_api"),
        "reasoning_effort": data.get("model_reasoning_effort"),
        "catalog_json": data.get("model_catalog_json"),
        "auto_compact_explicit": data.get("model_auto_compact_token_limit"),
        "context_window_explicit": data.get("model_context_window"),
    })
    return out


def read_catalog(cfg):
    if not cfg.get("catalog_json"):
        return {}
    path = cfg["catalog_json"]
    if not os.path.isabs(path):
        path = os.path.join(codex_home(), path)
    if not os.path.isfile(path):
        return {}
    try:
        models = json.load(open(path, "r", encoding="utf-8")).get("models", [])
    except Exception:
        return {}
    for m in models:
        if m.get("slug") == cfg.get("model"):
            return {
                "path": path,
                "context_window": m.get("context_window"),
                "effective_context_window_percent": m.get("effective_context_window_percent"),
                "default_reasoning_level": m.get("default_reasoning_level"),
                "truncation_policy": m.get("truncation_policy"),
            }
    return {"path": path}


def read_compaction(thread_id):
    db = os.path.join(codex_home(), "logs_2.sqlite")
    if not (thread_id and os.path.isfile(db)):
        return {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        row = None
        if thread_id:
            row = con.execute(
                "select feedback_log_body from logs where thread_id = ? "
                "and feedback_log_body like '%auto_compact_scope_limit%' order by id desc limit 1",
                (thread_id,)).fetchone()
        if not row:
            row = con.execute(
                "select feedback_log_body from logs where feedback_log_body like '%auto_compact_scope_limit%' "
                "order by id desc limit 1").fetchone()
        con.close()
    except Exception:
        return {}
    if not row:
        return {}
    body = row[0]
    out = {}
    for key in ("total_usage_tokens", "auto_compact_scope_tokens"):
        m = re.search(re.escape(key) + r"=(\d+)", body)
        if m:
            out[key] = int(m.group(1))
    for key in ("auto_compact_scope_limit", "full_context_window_limit"):
        m = re.search(re.escape(key) + r"=Some\((\d+)\)", body)
        if m:
            out[key] = int(m.group(1))
    m = re.search(r"auto_compact_limit_scope=(\w+)", body)
    if m:
        out["auto_compact_limit_scope"] = m.group(1)
    out["source"] = ("thread " + thread_id) if row else "latest log row"
    return out


def fmt(n):
    try:
        return format(int(n), ",")
    except Exception:
        return str(n)


def print_report(path, thread_id, usages, stats, cfg, cat, comp, recent):
    print("=" * 68)
    print("Codex Token Diagnostic")
    print("=" * 68)
    print("session : %s" % os.path.basename(path))
    print("thread  : %s" % (thread_id or "(unknown)"))
    print("requests: %d" % stats["count"])
    if cfg.get("exists"):
        print("model   : %s  (provider=%s name=%s effort=%s)" % (
            cfg.get("model"), cfg.get("model_provider"), cfg.get("provider_name"),
            cfg.get("reasoning_effort")))
        print("upstream: %s  wire_api=%s" % (cfg.get("base_url"), cfg.get("wire_api")))
    if cat.get("context_window"):
        eff = cat.get("effective_context_window_percent")
        hard = int(cat["context_window"] * eff / 100) if eff else None
        print("context : window=%s effective=%s%%%s" % (
            fmt(cat["context_window"]), eff, (" -> " + fmt(hard)) if hard else ""))
        if cat.get("truncation_policy"):
            print("truncate: %s" % cat["truncation_policy"])
    if comp:
        print("compact : auto_limit=%s hard_limit=%s scope=%s  (usage=%s, %s)" % (
            fmt(comp.get("auto_compact_scope_limit")), fmt(comp.get("full_context_window_limit")),
            comp.get("auto_compact_limit_scope"), fmt(comp.get("total_usage_tokens")),
            comp.get("source")))
    ov, rc = stats["overall"], stats["recent"]
    if ov:
        print("-" * 68)
        print("cache hit rate: recent %d = %.2f%%   overall = %.2f%%" % (
            rc["n"], rc["hit_rate"] * 100, ov["hit_rate"] * 100))
        print("totals      : input=%s cached=%s uncached=%s output=%s reasoning=%s" % (
            fmt(ov["input"]), fmt(ov["cached"]), fmt(ov["input"] - ov["cached"]),
            fmt(ov["output"]), fmt(ov["reasoning"])))
        print("recent avg miss: %s tokens/request" % fmt(round(rc["avg_miss"])))
    tag = "requests"
    print("-" * 68)
    print("%-24s %10s %10s %8s %8s %8s" % ("time", "input", "cached", "miss", "output", "reason"))
    for r in usages[-recent:]:
        print("%-24s %10s %10s %8s %8s %8s" % (
            (r["ts"] or "")[:19], fmt(r["input"]), fmt(r["cached"]),
            fmt(r["input"] - r["cached"]), fmt(r["output"]), fmt(r["reasoning"])))
    pairs = stats["pairs"]
    if pairs:
        print("-" * 68)
        print("cache-miss attribution  (predicted_new = d_input - prev_output)")
        print("%-24s %10s %9s %9s %8s %8s" % ("time", "d_input", "prev_out", "predicted", "actual", "delta"))
        for p in pairs[-recent:]:
            print("%-24s %10s %9s %9s %8s %8s" % (
                (p["ts"] or "")[:19], fmt(p["d_input"]), fmt(p["prev_output"]),
                fmt(p["predicted"]), fmt(p["actual"]), fmt(p["delta"])))
        v = stats["verdict"]
        print("-" * 68)
        print("median |actual miss| = %s   median |delta| = %s" % (
            fmt(round(v["median_abs_actual_miss"])), fmt(round(v["median_abs_delta"]))))
        if v["stable_prefix"]:
            print("verdict: STABLE PREFIX  ->  miss is mostly new tool output appended after last turn.")
            print("         optimize by shrinking large raw tool output, not by touching history.")
        else:
            print("verdict: POSSIBLE PREFIX REWRITE  ->  inspect the candidates below.")
        for p in v["prefix_break_candidates"][-5:]:
            print("         candidate @%s  delta=%s" % ((p["ts"] or "")[:19], fmt(p["delta"])))
    print("=" * 68)


def main():
    ap = argparse.ArgumentParser(description="Read-only Codex token / cache diagnostics.")
    ap.add_argument("--session", help="session id fragment or full jsonl path (default: most recent)")
    ap.add_argument("--recent", type=int, default=10, help="window size for tables and hit rate")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a text report")
    args = ap.parse_args()

    path = resolve_session(args.session)
    usages, thread_id = load_usages(path)
    stats = analyze(usages, args.recent)
    cfg = read_config()
    cat = read_catalog(cfg)
    comp = read_compaction(thread_id)

    if args.json:
        print(json.dumps({"session": path, "thread_id": thread_id, "usages": usages,
                          "stats": stats, "config": cfg, "catalog": cat,
                          "compaction": comp}, ensure_ascii=False, indent=2))
    else:
        print_report(path, thread_id, usages, stats, cfg, cat, comp, args.recent)


if __name__ == "__main__":
    main()