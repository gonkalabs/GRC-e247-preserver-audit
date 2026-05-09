# Stuck-pw "0.35x" Audit (Issue #2)

Prepared by Mike @ GonkaLabs.

Reproducer for the v0.2.12 migration bug raised by the Gonka Restitution Committee:

> *"MLNodes which were sampled as preserver nodes in epoch 247 (and might be
> next epoch until first PoC) causes lowered weight for this node (0.35x of
> the full weight)."*

`issue2_audit.py` runs end-to-end against the public chain RPC. No local DB,
no auth, Python 3.9+ stdlib only. Anyone can re-run it and get the same
numbers.

The narrative report with restitution tables is `RESTITUTION_REPORT.md`.

## Run it

```bash
python3 issue2_audit.py
```

Takes around 30 seconds (HTTP round-trips against the public node). Outputs
land in `./output/`.

Bottom line: **34 affected MLNodes** across 34 unique addresses, totaling
**10,701.39 GONKA** in restitution for the PoC fixed reward only.

## What the script does

For the baseline epoch (default 248, the last pre-upgrade snapshot) and the
Qwen subgroup it indexes every (participant, node) pair and reads its
`poc_weight`. Then it walks epochs 249..253 (post-upgrade) and classifies:

- **Stuck epoch**: `pw_E / pw_baseline` in `[0.95, 1.10]`. The value didn't
  move, so the node didn't run fresh PoC under v0.2.12 - its `MLNodeInfo.PocWeight`
  is still in pre-v0.2.12 scaled units.
- **Fix epoch**: `pw_E / pw_baseline >= 2.0`. The node ran fresh PoC under
  v0.2.12 and is now stored in raw nonces. Stuck-window ends here.
- Anything in `(1.10, 2.0)` is ambiguous and excluded for safety. The
  report has a separate section listing those for manual review.

For each (node, stuck epoch E) the per-epoch loss is:

```
lost_share_E  = (1 - WSF) * pw_baseline / totalFullWeight(E)
lost_ngonka_E = fixedEpochReward(E) * lost_share_E
```

`fixedEpochReward(E)` is the chain's own `initial * exp(decay_rate * (E - genesis))`
read from `inference.params.bitcoin_reward_params`. `totalFullWeight(E)` is
the observed sum of `weight` across the Qwen subgroup at epoch E.

Per-participant totals are aggregated. CSVs and a summary JSON go to
`./output/`.

## CLI flags

```
--rpc URL              Public chain RPC base (default http://node2.gonka.ai:8000)
--baseline-epoch N     Last pre-upgrade snapshot (default 248)
--post-start N         First post-upgrade epoch to scan (default 249)
--post-end N           Last post-upgrade epoch to scan (default 253; auto-clamped to current_epoch-1)
--model MODEL_ID       Subgroup model id (default Qwen/Qwen3-235B-A22B-Instruct-2507-FP8)
--max-stuck-ratio F    Upper bound for stuck classification (default 1.10)
--min-fix-ratio F      Lower bound for fixed classification (default 2.0)
```

## Outputs (`./output/`)

| file | rows | description |
|---|---|---|
| `issue2_per_node.csv` | 34 | per (participant, node): pw_baseline, stuck_epochs, fix_epoch, lost GONKA |
| `issue2_per_participant.csv` | 34 | aggregated per address: total lost GONKA, fixed-in-window flag |
| `issue2_summary.json` | - | cohort sizes + grand total |
| `issue2_log.txt` | - | full RPC trace from the last run |

## Sanity check against the chain directly

Pick any victim from `issue2_per_node.csv` (here the GRC member's case from
the report) and watch `poc_weight` and `confirmation_weight` flip across the
upgrade boundary:

```bash
ADDR=gonka16k03ze5ynkprsd4n6e5uzhthvu9jjk553rauqy

for E in 247 248 249 250 251; do
  curl -s --max-time 8 "http://node2.gonka.ai:8000/chain-api/productscience/inference/inference/epoch_group_data/${E}?model_id=Qwen%2FQwen3-235B-A22B-Instruct-2507-FP8" \
    | python3 -c "
import json, sys
d = json.load(sys.stdin)
g = d.get('epoch_group_data', {}) or {}
for vw in g.get('validation_weights') or []:
    if vw.get('member_address') != '$ADDR': continue
    w = int(vw.get('weight') or 0); c = int(vw.get('confirmation_weight') or 0)
    nodes = vw.get('ml_nodes') or []
    pw = nodes[0].get('poc_weight') if nodes else None
    print(f\"epoch=$E  weight={w:>6}  conf={c:>6}  conf/weight={(c/w if w else 0):.4f}  pw={pw}\")
    break
"
done
```

You should see `weight = 2685, conf = 0` (preserved, Issue #1) at e247,
`weight = 2685, conf = 2685` at e248 (last v0.2.11 reward formula), then
`weight = 2685, conf = 964` at e249 and e250 (`conf/weight = 0.359`, the
v0.2.12 reward formula applying WSF on top of a stale pre-scaled value).
That `0.359` is the bug - the chain re-applies `WeightScaleFactor` to a
value that already had it baked in.

## Dependencies

Python 3.9+ stdlib only. No `pip install` required.
