#!/usr/bin/env python3
"""
issue2_audit.py — Issue #2 (under-stored poc_weight, the "0.35x bug")

GRC task wording:
  "MLNodes which were sampled as preserver nodes in epoch 247 (and might be
   next epoch until first PoC) causes lowered weight for this node
   (0.35% [sic, 0.35x] of the full weight)"

What this script does:
  1. For each MLNode that was preserved (POC_SLOT=true) in epoch 247:
       a) record its anchor `poc_weight` in epoch 247 ("under-stored value")
       b) walk epochs 248, 249, ... to find the first epoch where the same
          node ran fresh PoC (POC_SLOT=false), record `fresh_pw` ("true value")
       c) compute the per-node ratio = anchor_pw / fresh_pw and the delta
          (fresh_pw - anchor_pw) — the weight units that were "missing" in
          every epoch the node remained preserved.
  2. For every preservation epoch (247 → first_fresh-1), compute the lost
     reward share for this node:
          lost_in_epoch = delta_pw * bitcoin_reward(epoch) / total_weight(epoch)
     where total_weight(epoch) is the observed sum of `weight` across all
     participants in that epoch's Qwen subgroup.
  3. Aggregate per (participant, node) and per participant.

Outputs (./output/):
  - issue2_per_node.csv         : one row per (participant, node) preserved in 247
  - issue2_per_participant.csv  : aggregated restitution per address
  - issue2_summary.json         : grand totals + cohort sizes
  - issue2_log.txt              : human-readable trace

Usage:
  python3 issue2_audit.py
  python3 issue2_audit.py --extended-cohort  # also include nodes preserved in 248 (not 247)
  python3 issue2_audit.py --bug-threshold 0.5
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
from dataclasses import dataclass, field, asdict
from decimal import Decimal, getcontext
from typing import Any

getcontext().prec = 40

DEFAULT_RPC = "http://node2.gonka.ai:8000"
QWEN_MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507-FP8"
ANCHOR_EPOCH = 247
LOOKAHEAD_EPOCHS = 12
BUG_RATIO_THRESHOLD = 0.5   # nodes with anchor_pw/fresh_pw < this are flagged Issue#2 victims
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
        except Exception as e:  # urlerror, httperror, socket.timeout (TimeoutError in 3.10+), etc.
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
        try: return Decimal(str(fp))
        except Exception: return Decimal(0)
    if isinstance(fp, dict):
        v = fp.get("value"); e = fp.get("exponent", 0)
        try: return Decimal(str(v)) * (Decimal(10) ** int(e))
        except Exception: return Decimal(0)
    return Decimal(0)


def fetch_inference_params(rpc: str) -> dict:
    return http_get(f"{rpc}/chain-api/productscience/inference/inference/params").get("params", {}) or {}


def fetch_epoch_group(rpc: str, epoch: int, model_id: str) -> dict | None:
    enc = urllib.parse.quote(model_id, safe="")
    url = f"{rpc}/chain-api/productscience/inference/inference/epoch_group_data/{epoch}?model_id={enc}"
    body = http_get(url)
    return (body.get("epoch_group_data") if body else None) or None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_preserved(timeslot_allocation: list | None) -> bool:
    """timeslot_allocation = [PRE_POC_SLOT, POC_SLOT]; POC_SLOT=true ⇒ preserved."""
    if not timeslot_allocation or len(timeslot_allocation) < 2:
        return False
    return bool(timeslot_allocation[1])


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
class NodeRestitutionRow:
    cohort: str                          # "preserved-in-247" or "preserved-in-248-only"
    participant_address: str
    model_id: str
    node_id: str
    anchor_epoch: int
    anchor_pw: int
    fresh_epoch: int | None              # first epoch where this node ran fresh PoC
    fresh_pw: int | None                 # observed fresh poc_weight (None if no follow-up)
    fresh_pw_estimated: int              # WSF-based estimate (anchor_pw / WSF) — ALWAYS computed for reference
    pw_ratio: str                        # anchor / fresh_observed (or anchor / estimated if observed missing)
    pw_delta_observed: int               # fresh_pw - anchor_pw  (0 if no observation)
    pw_delta_estimated: int              # fresh_pw_estimated - anchor_pw
    is_issue2_victim_observed: bool      # observed ratio < threshold AND fresh_pw > 0
    is_issue2_victim_estimated: bool     # estimate-based victim (always true if anchor_pw>0 and WSF<threshold)
    preserved_epochs: str                # comma-separated list, e.g. "247,248"
    n_preserved_epochs: int              # how many of those epochs we accounted for in compensation
    n_absent_epochs: int                 # epochs in window where node was missing from chain data
    lost_reward_observed_ngonka: int     # compensation using observed fresh_pw
    lost_reward_observed_gonka: str
    lost_reward_estimated_ngonka: int    # compensation using WSF-based estimate (covers indeterminate cases)
    lost_reward_estimated_gonka: str
    notes: str


@dataclass
class ParticipantRestitutionRow:
    cohort: str
    participant_address: str
    nodes_preserved_in_anchor: int
    nodes_observed_victims: int
    nodes_indeterminate: int             # no fresh follow-up — only WSF estimate available
    total_lost_observed_ngonka: int      # sum across nodes with observed fresh_pw
    total_lost_observed_gonka: str
    total_lost_estimated_ngonka: int     # sum using WSF estimate (covers indeterminate)
    total_lost_estimated_gonka: str
    flagged_node_ids: str


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def index_epoch(rpc: str, epoch: int, model_id: str, cache: dict) -> dict | None:
    """Returns epoch_group_data with caching."""
    if epoch in cache:
        return cache[epoch]
    grp = fetch_epoch_group(rpc, epoch, model_id)
    cache[epoch] = grp
    if grp:
        n_p = len(grp.get("validation_weights") or [])
        log(f"  fetched epoch {epoch} group ({n_p} participants)")
    return grp


def find_node_in_epoch(grp: dict | None, address: str, node_id: str) -> dict | None:
    if not grp:
        return None
    for vw in grp.get("validation_weights") or []:
        if vw.get("member_address") != address:
            continue
        for n in vw.get("ml_nodes") or []:
            if (n.get("node_id") or "") == node_id:
                return n
        return None
    return None


def epoch_total_weight(grp: dict | None) -> int:
    if not grp:
        return 0
    return sum(int(vw.get("weight") or 0) for vw in (grp.get("validation_weights") or []))


def trace_node(
    rpc: str, address: str, node_id: str, anchor_epoch: int, model_id: str,
    lookahead: int, cache: dict,
) -> tuple[list[int], int | None, int | None, int]:
    """
    Returns (preserved_epochs, fresh_epoch, fresh_pw, n_absent).

    preserved_epochs: list of epochs in [anchor_epoch, anchor_epoch+lookahead]
                     where the node was present AND POC_SLOT=true (including
                     anchor_epoch by construction). Gaps allowed if the node
                     was temporarily absent from chain data.
    fresh_epoch: first epoch in the scan window where the node was present with
                 POC_SLOT=false. None if never observed running fresh PoC
                 within the lookahead window.
    fresh_pw: poc_weight at fresh_epoch. None if fresh_epoch is None.
    n_absent: count of epochs in the scan window where the node was missing
              from chain data (informational).
    """
    preserved_epochs: list[int] = [anchor_epoch]
    n_absent = 0
    for e in range(anchor_epoch + 1, anchor_epoch + 1 + lookahead):
        grp = index_epoch(rpc, e, model_id, cache)
        node = find_node_in_epoch(grp, address, node_id)
        if node is None:
            n_absent += 1
            continue
        if is_preserved(node.get("timeslot_allocation")):
            preserved_epochs.append(e)
            continue
        return preserved_epochs, e, int(node.get("poc_weight") or 0), n_absent
    return preserved_epochs, None, None, n_absent


def audit(args) -> None:
    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)
    log(f"RPC = {args.rpc}")
    log(f"anchor_epoch = {args.epoch}")
    log(f"model = {args.model}")
    log(f"bug ratio threshold = {args.bug_threshold}")
    log(f"extended cohort? = {args.extended_cohort}")

    params = fetch_inference_params(args.rpc)
    poc_models = ((params.get("poc_params") or {}).get("models")) or []
    wsf = Decimal(0)
    for m in poc_models:
        if (m.get("model_id") or "") == args.model:
            wsf = fixed_to_decimal(m.get("weight_scale_factor"))
            break
    log(f"WeightScaleFactor (qwen) = {wsf}")

    # Clamp lookahead to current chain epoch — fetching epochs that don't exist yet wastes time on timeouts.
    cur_epoch = fetch_current_epoch(args.rpc)
    if cur_epoch > 0:
        max_lookahead = max(0, cur_epoch - args.epoch)
        if args.lookahead > max_lookahead:
            log(f"current chain epoch is {cur_epoch}; clamping lookahead from {args.lookahead} to {max_lookahead}")
            args.lookahead = max_lookahead

    cache: dict[int, dict] = {}
    anchor_grp = index_epoch(args.rpc, args.epoch, args.model, cache)
    if not anchor_grp:
        log(f"FATAL: anchor epoch {args.epoch} group missing")
        sys.exit(2)

    # Build the primary cohort: every (participant, node) preserved in anchor_epoch
    primary_cohort: list[tuple[str, str, int]] = []
    for vw in anchor_grp.get("validation_weights") or []:
        addr = vw.get("member_address") or ""
        for n in vw.get("ml_nodes") or []:
            if is_preserved(n.get("timeslot_allocation")):
                primary_cohort.append((addr, n.get("node_id") or "", int(n.get("poc_weight") or 0)))
    log(f"Primary cohort (preserved in {args.epoch}): {len(primary_cohort)} (participant, node) pairs")

    # Optional extended cohort: nodes preserved in anchor_epoch+1 but not anchor_epoch
    extended_cohort: list[tuple[str, str, int]] = []
    if args.extended_cohort:
        next_e = args.epoch + 1
        next_grp = index_epoch(args.rpc, next_e, args.model, cache)
        primary_keys = {(a, n) for (a, n, _) in primary_cohort}
        if next_grp:
            for vw in next_grp.get("validation_weights") or []:
                addr = vw.get("member_address") or ""
                for n in vw.get("ml_nodes") or []:
                    if is_preserved(n.get("timeslot_allocation")):
                        nid = n.get("node_id") or ""
                        if (addr, nid) not in primary_keys:
                            extended_cohort.append((addr, nid, int(n.get("poc_weight") or 0)))
            log(f"Extended cohort (preserved in {next_e} but not {args.epoch}): {len(extended_cohort)} pairs")

    # Pre-fetch consecutive epochs we'll need for total_weight reads
    log(f"Pre-fetching epochs {args.epoch}..{args.epoch + args.lookahead}")
    for e in range(args.epoch, args.epoch + args.lookahead + 1):
        index_epoch(args.rpc, e, args.model, cache)

    # Process every (participant, node) → restitution row
    node_rows: list[NodeRestitutionRow] = []

    def process_pair(cohort: str, addr: str, nid: str, anchor_pw: int, anchor_epoch: int) -> None:
        preserved_epochs, fresh_epoch, fresh_pw, n_absent = trace_node(
            args.rpc, addr, nid, anchor_epoch, args.model, args.lookahead, cache,
        )
        # WSF estimate: pre-v0.2.12 stored values were already × WSF, so estimated true_pw = anchor_pw / WSF.
        # We always compute this so the GRC has a number even for nodes with no observed fresh follow-up.
        if wsf > 0 and anchor_pw > 0:
            fresh_pw_est = int((Decimal(anchor_pw) / wsf).to_integral_value())
        else:
            fresh_pw_est = anchor_pw

        # Observed metrics
        ratio_obs = (Decimal(anchor_pw) / Decimal(fresh_pw)) if (fresh_pw and fresh_pw > 0) else Decimal(0)
        delta_obs = (fresh_pw - anchor_pw) if (fresh_pw and fresh_pw > 0) else 0
        is_victim_obs = (fresh_pw is not None and fresh_pw > 0 and ratio_obs < Decimal(str(args.bug_threshold)))

        # Estimated metrics (WSF-based)
        delta_est = max(fresh_pw_est - anchor_pw, 0)
        # An estimated victim if the WSF itself is < threshold (which it is at 0.3593 < 0.5)
        # AND we have an anchor_pw to scale.
        is_victim_est = (anchor_pw > 0 and wsf > 0 and wsf < Decimal(str(args.bug_threshold)))

        # Compute compensation:
        #  - lost_obs: uses observed delta when we have observed fresh_pw evidence the node was under-stored
        #  - lost_est: BEST-AVAILABLE compensation per node:
        #      * if observed (fresh_pw is not None): use observed delta (could be 0 if not actually a victim)
        #      * if no observation: fall back to WSF estimate (assume true_pw = anchor_pw / WSF)
        #    The "lost_est" bucket is therefore: observed-where-known + estimated-where-indeterminate
        lost_obs = 0
        lost_est = 0
        accounted_epochs = 0
        notes_parts: list[str] = []
        # Decide which delta to use for the est bucket:
        #   observed available -> use observed (whether 0 or positive)
        #   no observation     -> fall back to WSF estimate
        if fresh_pw is not None:
            delta_for_est = delta_obs
            est_basis = "observed"
        else:
            delta_for_est = delta_est
            est_basis = "wsf_estimate"

        for e in preserved_epochs:
            grp = cache.get(e)
            tw = epoch_total_weight(grp)
            if tw <= 0:
                notes_parts.append(f"e{e}:no_total_weight")
                continue
            er = compute_epoch_reward_ngonka(params, e)
            if is_victim_obs and delta_obs > 0:
                lost_obs += int((Decimal(delta_obs) * Decimal(er) / Decimal(tw)).to_integral_value())
            if delta_for_est > 0:
                lost_est += int((Decimal(delta_for_est) * Decimal(er) / Decimal(tw)).to_integral_value())
            accounted_epochs += 1
        notes_parts.append(f"est_basis={est_basis}")
        if fresh_pw is None:
            notes_parts.append("no_observed_fresh_followup_within_lookahead")
        if n_absent > 0:
            notes_parts.append(f"absent_in_{n_absent}_epochs_within_window")

        node_rows.append(NodeRestitutionRow(
            cohort=cohort,
            participant_address=addr,
            model_id=args.model,
            node_id=nid,
            anchor_epoch=anchor_epoch,
            anchor_pw=anchor_pw,
            fresh_epoch=fresh_epoch,
            fresh_pw=fresh_pw,
            fresh_pw_estimated=fresh_pw_est,
            pw_ratio=str(ratio_obs.quantize(Decimal("0.000001"))) if fresh_pw else str((Decimal(anchor_pw) / Decimal(fresh_pw_est)).quantize(Decimal("0.000001"))) if fresh_pw_est else "0",
            pw_delta_observed=delta_obs,
            pw_delta_estimated=delta_est,
            is_issue2_victim_observed=is_victim_obs,
            is_issue2_victim_estimated=is_victim_est,
            preserved_epochs=",".join(str(x) for x in preserved_epochs),
            n_preserved_epochs=accounted_epochs,
            n_absent_epochs=n_absent,
            lost_reward_observed_ngonka=lost_obs,
            lost_reward_observed_gonka=str((Decimal(lost_obs) / Decimal(10) ** 9).quantize(Decimal("0.000001"))),
            lost_reward_estimated_ngonka=lost_est,
            lost_reward_estimated_gonka=str((Decimal(lost_est) / Decimal(10) ** 9).quantize(Decimal("0.000001"))),
            notes=";".join(notes_parts),
        ))

    log(f"Processing {len(primary_cohort)} primary-cohort nodes ...")
    for (addr, nid, pw) in primary_cohort:
        process_pair("preserved-in-247", addr, nid, pw, args.epoch)
    if extended_cohort:
        log(f"Processing {len(extended_cohort)} extended-cohort nodes ...")
        for (addr, nid, pw) in extended_cohort:
            process_pair("preserved-in-248-only", addr, nid, pw, args.epoch + 1)

    # Aggregate per participant
    by_addr: dict[tuple[str, str], dict] = {}
    for nr in node_rows:
        key = (nr.cohort, nr.participant_address)
        agg = by_addr.setdefault(key, {
            "nodes_preserved": 0,
            "nodes_observed_victims": 0,
            "nodes_indeterminate": 0,
            "total_lost_obs_ngonka": 0,
            "total_lost_est_ngonka": 0,
            "flagged_nodes": [],
        })
        agg["nodes_preserved"] += 1
        agg["total_lost_obs_ngonka"] += nr.lost_reward_observed_ngonka
        agg["total_lost_est_ngonka"] += nr.lost_reward_estimated_ngonka
        if nr.is_issue2_victim_observed:
            agg["nodes_observed_victims"] += 1
            agg["flagged_nodes"].append(nr.node_id[:18])
        elif nr.fresh_pw is None:
            agg["nodes_indeterminate"] += 1
            agg["flagged_nodes"].append(nr.node_id[:18] + "(est)")

    participant_rows: list[ParticipantRestitutionRow] = []
    for (cohort, addr), agg in by_addr.items():
        participant_rows.append(ParticipantRestitutionRow(
            cohort=cohort,
            participant_address=addr,
            nodes_preserved_in_anchor=agg["nodes_preserved"],
            nodes_observed_victims=agg["nodes_observed_victims"],
            nodes_indeterminate=agg["nodes_indeterminate"],
            total_lost_observed_ngonka=agg["total_lost_obs_ngonka"],
            total_lost_observed_gonka=str((Decimal(agg["total_lost_obs_ngonka"]) / Decimal(10) ** 9).quantize(Decimal("0.000001"))),
            total_lost_estimated_ngonka=agg["total_lost_est_ngonka"],
            total_lost_estimated_gonka=str((Decimal(agg["total_lost_est_ngonka"]) / Decimal(10) ** 9).quantize(Decimal("0.000001"))),
            flagged_node_ids=",".join(agg["flagged_nodes"]),
        ))

    # Stats
    obs_victims = [r for r in node_rows if r.is_issue2_victim_observed]
    indeterminate = [r for r in node_rows if r.fresh_pw is None]
    def gonka(n: int) -> str:
        return str((Decimal(n) / Decimal(10) ** 9).quantize(Decimal("0.000001")))
    sum_obs = sum(r.lost_reward_observed_ngonka for r in node_rows)
    sum_est = sum(r.lost_reward_estimated_ngonka for r in node_rows)
    summary = {
        "anchor_epoch": args.epoch,
        "model_id": args.model,
        "weight_scale_factor": str(wsf),
        "bug_ratio_threshold": args.bug_threshold,
        "primary_cohort_size": len(primary_cohort),
        "extended_cohort_size": len(extended_cohort),
        "total_nodes_processed": len(node_rows),

        "observed_victims_total": len(obs_victims),
        "observed_victims_in_primary_cohort": len([r for r in obs_victims if r.cohort == "preserved-in-247"]),
        "observed_victims_in_extended_cohort": len([r for r in obs_victims if r.cohort == "preserved-in-248-only"]),
        "observed_victim_pw_ratios": sorted(set(r.pw_ratio for r in obs_victims))[:30],
        "total_lost_observed_gonka": gonka(sum_obs),
        "lost_observed_in_primary_cohort_gonka": gonka(sum(r.lost_reward_observed_ngonka for r in node_rows if r.cohort == "preserved-in-247")),
        "lost_observed_in_extended_cohort_gonka": gonka(sum(r.lost_reward_observed_ngonka for r in node_rows if r.cohort == "preserved-in-248-only")),

        "indeterminate_nodes_total": len(indeterminate),
        "indeterminate_in_primary_cohort": len([r for r in indeterminate if r.cohort == "preserved-in-247"]),
        "indeterminate_in_extended_cohort": len([r for r in indeterminate if r.cohort == "preserved-in-248-only"]),

        "total_lost_estimated_gonka": gonka(sum_est),
        "lost_estimated_in_primary_cohort_gonka": gonka(sum(r.lost_reward_estimated_ngonka for r in node_rows if r.cohort == "preserved-in-247")),
        "lost_estimated_in_extended_cohort_gonka": gonka(sum(r.lost_reward_estimated_ngonka for r in node_rows if r.cohort == "preserved-in-248-only")),
    }
    log("Summary: " + json.dumps(summary, indent=2))

    # Write outputs
    def write_csv(path: str, rows: list, fieldnames: list[str]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                d = asdict(r)
                d = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in d.items()}
                w.writerow(d)

    write_csv(os.path.join(OUT_DIR, "issue2_per_node.csv"),
              node_rows, list(NodeRestitutionRow.__dataclass_fields__.keys()))
    write_csv(os.path.join(OUT_DIR, "issue2_per_participant.csv"),
              participant_rows, list(ParticipantRestitutionRow.__dataclass_fields__.keys()))
    with open(os.path.join(OUT_DIR, "issue2_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    log(f"\nWrote:")
    log(f"  {os.path.join(OUT_DIR, 'issue2_per_node.csv')}  ({len(node_rows)} rows)")
    log(f"  {os.path.join(OUT_DIR, 'issue2_per_participant.csv')}  ({len(participant_rows)} rows)")
    log(f"  {os.path.join(OUT_DIR, 'issue2_summary.json')}")
    log(f"  {LOG_PATH}")

    # Print restitution table to stdout — both observed (lower bound) and estimated (full)
    pr_with_loss = [r for r in participant_rows if r.total_lost_estimated_ngonka > 0]
    pr_sorted = sorted(pr_with_loss, key=lambda r: r.total_lost_estimated_ngonka, reverse=True)
    print("\n=== Issue #2 RESTITUTION TABLE ===")
    print("OBSERVED  = compensation using observed fresh_pw (only nodes that ran fresh PoC again)")
    print("ESTIMATED = compensation assuming true_pw = anchor_pw / WeightScaleFactor (covers indeterminate)")
    print()
    print(f"{'cohort':<24}  {'address':<46}  {'#nodes':>6}  {'#obs_v':>6}  {'#indet':>6}  {'OBS_GONKA':>14}  {'EST_GONKA':>14}")
    grand_obs = grand_est = 0
    for r in pr_sorted:
        print(f"{r.cohort:<24}  {r.participant_address:<46}  {r.nodes_preserved_in_anchor:>6}  {r.nodes_observed_victims:>6}  {r.nodes_indeterminate:>6}  {r.total_lost_observed_gonka:>14}  {r.total_lost_estimated_gonka:>14}")
        grand_obs += r.total_lost_observed_ngonka
        grand_est += r.total_lost_estimated_ngonka
    print()
    print(f"Participants with non-zero estimated loss: {len(pr_sorted)}")
    print(f"Grand total OBSERVED restitution:  {grand_obs / 1e9:>14.6f} GONKA")
    print(f"Grand total ESTIMATED restitution: {grand_est / 1e9:>14.6f} GONKA  (covers indeterminate cases via WSF)")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rpc", default=DEFAULT_RPC)
    p.add_argument("--epoch", type=int, default=ANCHOR_EPOCH, help="Anchor epoch (default 247)")
    p.add_argument("--model", default=QWEN_MODEL)
    p.add_argument("--lookahead", type=int, default=LOOKAHEAD_EPOCHS)
    p.add_argument("--bug-threshold", type=float, default=BUG_RATIO_THRESHOLD,
                   help="Flag node as Issue#2 victim if anchor_pw/fresh_pw < this (default 0.5)")
    p.add_argument("--extended-cohort", action="store_true",
                   help="Also include nodes preserved in anchor+1 but not anchor (potential extended scope)")
    return p.parse_args()


def main() -> int:
    audit(parse_args())
    return 0


if __name__ == "__main__":
    sys.exit(main())
