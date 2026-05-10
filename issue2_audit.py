#!/usr/bin/env python3
"""
issue2_audit.py - Issue #2 (the "0.35x bug" introduced by the v0.2.12 upgrade)

Mechanism (verified on chain):
  - Pre-v0.2.12 stored each MLNode's `poc_weight` in the consensus-scaled unit
    (= raw_nonces x WeightScaleFactor). The reward formula did not multiply by
    WSF a second time.
  - v0.2.12 (active from epoch 249 onward) flipped the convention: stored values
    are now raw nonces, and `confirmation_weight` is computed as
    WSF x sum(poc_weight). vw.Weight stays at sum(poc_weight).
  - For any MLNode that did NOT re-run PoC under the new code (i.e. it stayed
    "stuck" with the pre-v0.2.12 stored value), the chain interprets that
    stale, already-scaled number as raw and multiplies by WSF again. So its
    consensus contribution becomes WSF x pw_stuck instead of pw_stuck (the
    intended consensus weight).
  - Concretely, for the Qwen subgroup (WSF = 0.3593) the affected node
    contributes ~36% of what its hardware actually produced -> the "0.35x" pattern
    that the GRC ticket describes.

Detection (Cohort B):
  Anchor is the LAST pre-upgrade snapshot, epoch 248. For every (participant, node)
  pair seen there, we walk epochs 249..max_epoch:
    - "stuck epoch": pw_E / pw_248  in [0.95, 1.10]   (= same value, no re-PoC)
    - "fix epoch":   pw_E / pw_248 >= 2.0             (= properly re-PoC'd in raw units)
  We compensate every stuck epoch up to (but excluding) the first fix epoch, or
  the last observed epoch if the node is never fixed in the scan window.

Compensation per (node, stuck epoch E), under the GRC "broad restitution"
policy:
    lost_share_E   = (1 - WSF) * pw_stuck / totalConfirmationWeight(E)
    lost_ngonka_E  = fixed_epoch_reward(E) * lost_share_E

  Rationale: observed participant payouts in the affected epochs reconcile
  against sum(confirmation_weight), not sum(weight). For example, in epoch 249:
    1003 / 742426 * 287106 ~= 388 GNK
  In the "fair" world the node's pw would have been pw_stuck/WSF (raw), so its
  consensus contribution would have been pw_stuck (= WSF * pw_stuck/WSF). The
  per-epoch shortfall in the numerator is therefore (pw_stuck - WSF*pw_stuck)
  = (1-WSF) * pw_stuck.

Outputs (./output/):
  - issue2_per_node.csv         : one row per (participant, node) flagged as stuck
  - issue2_per_participant.csv  : aggregated restitution per address
  - issue2_summary.json         : cohort sizes + grand total
  - issue2_log.txt              : human-readable trace

Usage:
  python3 issue2_audit.py
  python3 issue2_audit.py --baseline-epoch 248 --post-start 249 --post-end 253
  python3 issue2_audit.py --max-stuck-ratio 1.10 --min-fix-ratio 2.0
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, asdict
from decimal import Decimal, getcontext
from typing import Any

getcontext().prec = 40

DEFAULT_RPC = "http://node2.gonka.ai:8000"
QWEN_MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507-FP8"
BASELINE_EPOCH = 248          # last pre-upgrade snapshot
POST_START_EPOCH = 249        # first post-upgrade epoch (where v0.2.12 reward formula activates)
POST_END_EPOCH = 253          # default scan upper bound; clamped to current_epoch-1 at runtime
MAX_STUCK_RATIO = 1.10        # pw_E / pw_baseline in [0.95, this] => stuck
MIN_STUCK_RATIO = 0.95
MIN_FIX_RATIO = 2.0           # pw_E / pw_baseline >= this => the node ran fresh PoC under v0.2.12
HTTP_TIMEOUT = 15
HTTP_RETRIES = 3

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
os.makedirs(OUT_DIR, exist_ok=True)
LOG_PATH = os.path.join(OUT_DIR, "issue2_log.txt")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get(url: str) -> dict:
    """Returns parsed JSON. On any error (timeout, HTTP, network), returns {}."""
    last_err: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(0.4 * attempt)
    log(f"  WARN: GET {url} failed after {HTTP_RETRIES} retries: {last_err}")
    return {}


def fetch_current_epoch(rpc: str) -> int:
    body = http_get(f"{rpc}/chain-api/productscience/inference/inference/get_current_epoch")
    try:
        return int(body.get("epoch") or 0)
    except (TypeError, ValueError):
        return 0


def fixed_to_decimal(fp: Any) -> Decimal:
    if fp is None:
        return Decimal(0)
    if isinstance(fp, Decimal):
        return fp
    if isinstance(fp, (int, float, str)):
        try:
            return Decimal(str(fp))
        except Exception:
            return Decimal(0)
    if isinstance(fp, dict):
        v = fp.get("value")
        e = fp.get("exponent", 0)
        try:
            return Decimal(str(v)) * (Decimal(10) ** int(e))
        except Exception:
            return Decimal(0)
    return Decimal(0)


def fetch_inference_params(rpc: str) -> dict:
    return http_get(f"{rpc}/chain-api/productscience/inference/inference/params").get("params", {}) or {}


def fetch_epoch_group(rpc: str, epoch: int, model_id: str) -> dict | None:
    enc = urllib.parse.quote(model_id, safe="")
    url = f"{rpc}/chain-api/productscience/inference/inference/epoch_group_data/{epoch}?model_id={enc}"
    body = http_get(url)
    return (body.get("epoch_group_data") if body else None) or None


def compute_epoch_reward_ngonka(params: dict, epoch_index: int) -> int:
    br = params.get("bitcoin_reward_params") or {}
    initial = int(str(br.get("initial_epoch_reward") or "0"))
    decay = fixed_to_decimal(br.get("decay_rate"))
    genesis_epoch = int(str(br.get("genesis_epoch") or "1"))
    if initial <= 0:
        return 0
    elapsed = max(0, epoch_index - genesis_epoch)
    factor = Decimal(math.exp(float(decay) * elapsed))
    return int((Decimal(initial) * factor).to_integral_value())


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class NodeRow:
    participant_address: str
    model_id: str
    node_id: str
    pw_baseline: int                 # pw at baseline_epoch (last pre-upgrade snapshot)
    baseline_epoch: int
    first_post_epoch: int            # first post-upgrade epoch the node was seen
    pw_first_post: int
    ratio_first_post: str            # pw_first_post / pw_baseline
    stuck_epochs: str                # CSV of post-upgrade epochs where node was stuck
    n_stuck_epochs: int
    fix_epoch: int | None            # first epoch where pw jumped >= MIN_FIX_RATIO
    pw_at_fix: int | None
    expected_pw_under_v0_2_12: int   # = pw_baseline / WSF, what raw value SHOULD have been
    denominator_mode: str
    epoch_total_confirmation_weights: str
    epoch_rewards_gonka: str
    lost_by_epoch_gonka: str
    lost_ngonka: int
    lost_gonka: str
    notes: str


@dataclass
class ParticipantRow:
    participant_address: str
    n_stuck_nodes: int
    stuck_node_ids: str
    first_stuck_epoch: int
    last_stuck_epoch: int
    fixed_in_window: bool             # True if all stuck nodes have a fix_epoch within scan window
    total_lost_ngonka: int
    total_lost_gonka: str


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def index_epoch(rpc: str, epoch: int, model_id: str, cache: dict) -> dict | None:
    if epoch in cache:
        return cache[epoch]
    grp = fetch_epoch_group(rpc, epoch, model_id)
    cache[epoch] = grp
    if grp:
        n_p = len(grp.get("validation_weights") or [])
        log(f"  fetched epoch {epoch} ({n_p} participants)")
    return grp


def epoch_total_confirmation_weight(grp: dict | None) -> int:
    if not grp:
        return 0
    return sum(int(vw.get("confirmation_weight") or 0) for vw in (grp.get("validation_weights") or []))


def collect_pw(grp: dict | None) -> dict[tuple[str, str], int]:
    """Returns {(address, node_id): poc_weight} for one epoch's group."""
    out: dict[tuple[str, str], int] = {}
    if not grp:
        return out
    for vw in grp.get("validation_weights") or []:
        addr = vw.get("member_address") or ""
        for n in vw.get("ml_nodes") or []:
            nid = n.get("node_id") or ""
            out[(addr, nid)] = int(n.get("poc_weight") or 0)
    return out


def audit(args) -> None:
    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)
    log(f"RPC                = {args.rpc}")
    log(f"baseline_epoch     = {args.baseline_epoch}  (last pre-upgrade snapshot)")
    log(f"post_window        = [{args.post_start}..{args.post_end}]")
    log(f"max_stuck_ratio    = {args.max_stuck_ratio}")
    log(f"min_fix_ratio      = {args.min_fix_ratio}")
    log(f"model              = {args.model}")

    cur_epoch = fetch_current_epoch(args.rpc)
    if cur_epoch > 0 and args.post_end > cur_epoch - 1:
        log(f"current chain epoch is {cur_epoch}; clamping post_end {args.post_end} -> {cur_epoch - 1}")
        args.post_end = cur_epoch - 1

    params = fetch_inference_params(args.rpc)
    poc_models = ((params.get("poc_params") or {}).get("models")) or []
    wsf = Decimal(0)
    for m in poc_models:
        if (m.get("model_id") or "") == args.model:
            wsf = fixed_to_decimal(m.get("weight_scale_factor"))
            break
    log(f"WeightScaleFactor  = {wsf}")
    if wsf <= 0:
        log("FATAL: WSF could not be resolved; cannot compute compensation")
        sys.exit(2)

    cache: dict[int, dict] = {}
    epochs_needed = list(range(args.baseline_epoch, args.post_end + 1))
    log(f"Pre-fetching epochs {epochs_needed}")
    for e in epochs_needed:
        index_epoch(args.rpc, e, args.model, cache)

    baseline_grp = cache.get(args.baseline_epoch)
    if not baseline_grp:
        log(f"FATAL: baseline epoch {args.baseline_epoch} group missing")
        sys.exit(2)

    baseline_pw = collect_pw(baseline_grp)
    log(f"Baseline cohort (every node seen at epoch {args.baseline_epoch}): {len(baseline_pw)} (participant, node) pairs")

    post_pw_by_epoch: dict[int, dict[tuple[str, str], int]] = {}
    for e in range(args.post_start, args.post_end + 1):
        post_pw_by_epoch[e] = collect_pw(cache.get(e))

    node_rows: list[NodeRow] = []
    for (addr, nid), pw_base in baseline_pw.items():
        if pw_base <= 0:
            continue

        first_post_epoch = None
        pw_first_post = None
        stuck_epochs: list[int] = []
        fix_epoch = None
        pw_at_fix = None

        for e in range(args.post_start, args.post_end + 1):
            pw = post_pw_by_epoch[e].get((addr, nid))
            if pw is None:
                continue
            if first_post_epoch is None:
                first_post_epoch = e
                pw_first_post = pw
            ratio = pw / pw_base if pw_base else 0
            if MIN_STUCK_RATIO <= ratio <= args.max_stuck_ratio:
                stuck_epochs.append(e)
            elif ratio >= args.min_fix_ratio:
                fix_epoch = e
                pw_at_fix = pw
                break

        if not stuck_epochs:
            continue

        expected_raw = int((Decimal(pw_base) / wsf).to_integral_value())

        epoch_reward = compute_epoch_reward_ngonka
        lost = 0
        notes_parts: list[str] = []
        epoch_denominators: list[str] = []
        epoch_rewards: list[str] = []
        lost_by_epoch: list[str] = []
        for e in stuck_epochs:
            tcw = epoch_total_confirmation_weight(cache.get(e))
            if tcw <= 0:
                notes_parts.append(f"e{e}:no_total_confirmation_weight")
                continue
            er = epoch_reward(params, e)
            lost_e = int((Decimal(pw_base) * (Decimal(1) - wsf) * Decimal(er) / Decimal(tcw)).to_integral_value())
            lost += lost_e
            epoch_denominators.append(f"{e}:{tcw}")
            epoch_rewards.append(f"{e}:{(Decimal(er) / Decimal(10) ** 9).quantize(Decimal('0.000001'))}")
            lost_by_epoch.append(f"{e}:{(Decimal(lost_e) / Decimal(10) ** 9).quantize(Decimal('0.000001'))}")

        if fix_epoch is None:
            notes_parts.append("not_fixed_within_scan_window")

        ratio_first = (Decimal(pw_first_post or 0) / Decimal(pw_base)).quantize(Decimal("0.0001"))

        node_rows.append(NodeRow(
            participant_address=addr,
            model_id=args.model,
            node_id=nid,
            pw_baseline=pw_base,
            baseline_epoch=args.baseline_epoch,
            first_post_epoch=first_post_epoch or 0,
            pw_first_post=pw_first_post or 0,
            ratio_first_post=str(ratio_first),
            stuck_epochs=",".join(str(x) for x in stuck_epochs),
            n_stuck_epochs=len(stuck_epochs),
            fix_epoch=fix_epoch,
            pw_at_fix=pw_at_fix,
            expected_pw_under_v0_2_12=expected_raw,
            denominator_mode="raw_total_confirmation_weight",
            epoch_total_confirmation_weights=";".join(epoch_denominators),
            epoch_rewards_gonka=";".join(epoch_rewards),
            lost_by_epoch_gonka=";".join(lost_by_epoch),
            lost_ngonka=lost,
            lost_gonka=str((Decimal(lost) / Decimal(10) ** 9).quantize(Decimal("0.000001"))),
            notes=";".join(notes_parts) if notes_parts else "",
        ))

    log(f"Cohort B (stuck pw across upgrade): {len(node_rows)} nodes "
        f"across {len({r.participant_address for r in node_rows})} addresses")

    by_addr: dict[str, dict] = defaultdict(lambda: {
        "n_stuck": 0, "node_ids": [], "first_stuck": 10**9, "last_stuck": 0,
        "fixed_in_window": True, "total_lost": 0,
    })
    for nr in node_rows:
        a = by_addr[nr.participant_address]
        a["n_stuck"] += 1
        a["node_ids"].append(nr.node_id[:24])
        first_e = int(nr.stuck_epochs.split(",")[0])
        last_e = int(nr.stuck_epochs.split(",")[-1])
        if first_e < a["first_stuck"]:
            a["first_stuck"] = first_e
        if last_e > a["last_stuck"]:
            a["last_stuck"] = last_e
        if nr.fix_epoch is None:
            a["fixed_in_window"] = False
        a["total_lost"] += nr.lost_ngonka

    participant_rows: list[ParticipantRow] = []
    for addr, agg in by_addr.items():
        participant_rows.append(ParticipantRow(
            participant_address=addr,
            n_stuck_nodes=agg["n_stuck"],
            stuck_node_ids=",".join(agg["node_ids"]),
            first_stuck_epoch=agg["first_stuck"],
            last_stuck_epoch=agg["last_stuck"],
            fixed_in_window=agg["fixed_in_window"],
            total_lost_ngonka=agg["total_lost"],
            total_lost_gonka=str((Decimal(agg["total_lost"]) / Decimal(10) ** 9).quantize(Decimal("0.000001"))),
        ))

    grand_lost = sum(nr.lost_ngonka for nr in node_rows)
    fixed_in_window = sum(1 for nr in node_rows if nr.fix_epoch is not None)

    summary = {
        "baseline_epoch": args.baseline_epoch,
        "post_window": [args.post_start, args.post_end],
        "model_id": args.model,
        "weight_scale_factor": str(wsf),
        "max_stuck_ratio": args.max_stuck_ratio,
        "min_fix_ratio": args.min_fix_ratio,
        "denominator_mode": "raw_total_confirmation_weight",
        "restitution_policy": "broad_include_misses_and_invalidations",
        "baseline_cohort_size": len(baseline_pw),
        "stuck_node_count": len(node_rows),
        "stuck_address_count": len(participant_rows),
        "stuck_nodes_fixed_in_window": fixed_in_window,
        "stuck_nodes_unfixed_in_window": len(node_rows) - fixed_in_window,
        "grand_total_lost_ngonka": grand_lost,
        "grand_total_lost_gonka": str((Decimal(grand_lost) / Decimal(10) ** 9).quantize(Decimal("0.000001"))),
    }
    log("Summary: " + json.dumps(summary, indent=2))

    def write_csv(path: str, rows: list, fieldnames: list[str]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                d = asdict(r)
                d = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in d.items()}
                w.writerow(d)

    write_csv(os.path.join(OUT_DIR, "issue2_per_node.csv"),
              node_rows, list(NodeRow.__dataclass_fields__.keys()))
    write_csv(os.path.join(OUT_DIR, "issue2_per_participant.csv"),
              participant_rows, list(ParticipantRow.__dataclass_fields__.keys()))
    with open(os.path.join(OUT_DIR, "issue2_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    log("\nWrote:")
    log(f"  {os.path.join(OUT_DIR, 'issue2_per_node.csv')}  ({len(node_rows)} rows)")
    log(f"  {os.path.join(OUT_DIR, 'issue2_per_participant.csv')}  ({len(participant_rows)} rows)")
    log(f"  {os.path.join(OUT_DIR, 'issue2_summary.json')}")
    log(f"  {LOG_PATH}")

    pr_sorted = sorted(participant_rows, key=lambda r: r.total_lost_ngonka, reverse=True)
    print("\n=== Issue #2 RESTITUTION TABLE (Cohort B = stuck pre-scaled pw) ===")
    print(f"baseline epoch   = {args.baseline_epoch}  (last pre-upgrade snapshot)")
    print(f"post-upgrade win = [{args.post_start}..{args.post_end}]")
    print(f"WSF (Qwen)       = {wsf}")
    print("denominator      = raw total confirmation_weight")
    print("policy           = broad; include affected nodes even with misses/invalidation")
    print()
    print(f"{'address':<46}  {'#nodes':>6}  {'stuck_eps':>9}  {'fixed?':>7}  {'lost_GONKA':>14}")
    for r in pr_sorted:
        eps_range = f"{r.first_stuck_epoch}-{r.last_stuck_epoch}" if r.first_stuck_epoch != r.last_stuck_epoch else str(r.first_stuck_epoch)
        print(f"{r.participant_address:<46}  {r.n_stuck_nodes:>6}  {eps_range:>9}  {('yes' if r.fixed_in_window else 'no'):>7}  {r.total_lost_gonka:>14}")
    print()
    print(f"Stuck nodes:        {len(node_rows)} across {len(participant_rows)} unique addresses")
    print(f"Fixed in window:    {fixed_in_window} / {len(node_rows)}")
    print(f"Grand total lost:   {grand_lost / 1e9:>14.6f} GONKA")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rpc", default=DEFAULT_RPC)
    p.add_argument("--baseline-epoch", type=int, default=BASELINE_EPOCH,
                   help="Last pre-upgrade snapshot (default 248)")
    p.add_argument("--post-start", type=int, default=POST_START_EPOCH,
                   help="First post-upgrade epoch (default 249, where v0.2.12 reward formula activates)")
    p.add_argument("--post-end", type=int, default=POST_END_EPOCH,
                   help="Last post-upgrade epoch to scan (default 253; clamped to current_epoch-1)")
    p.add_argument("--model", default=QWEN_MODEL)
    p.add_argument("--max-stuck-ratio", type=float, default=MAX_STUCK_RATIO,
                   help=f"pw_post/pw_baseline <= this AND >= {MIN_STUCK_RATIO} => stuck (default {MAX_STUCK_RATIO})")
    p.add_argument("--min-fix-ratio", type=float, default=MIN_FIX_RATIO,
                   help=f"pw_post/pw_baseline >= this => properly re-PoC'd (default {MIN_FIX_RATIO})")
    return p.parse_args()


def main() -> int:
    audit(parse_args())
    return 0


if __name__ == "__main__":
    sys.exit(main())
