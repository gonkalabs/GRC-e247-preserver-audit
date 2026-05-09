# Epoch 247 Preserver-Node Audit (Issue #2)

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
python3 issue2_audit.py --extended-cohort
```

Takes around 30 seconds (HTTP round-trips against the public node). Outputs
land in `./output/`.

Bottom line: 30 affected MLNodes (12 with hard observed evidence, 18 with a
WSF-based estimate), about 42,298 GONKA in total restitution.

## What the script does

For the anchor epoch (default 247) and the Qwen subgroup it walks every
MLNode that had `timeslot_allocation[POC_SLOT] == true`. With
`--extended-cohort` it also adds nodes preserved in epoch 248 that were not
preserved in 247 (the GRC's *"and might be next epoch until first PoC"*
clause).

For every node in the cohort it reads `anchor_pw` (the under-stored value),
walks forward up to `--lookahead` epochs to find the first epoch where the
same node ran fresh PoC (`POC_SLOT == false`, i.e. the chain wrote a fresh
`poc_weight`), and tracks every epoch the node remained preserved in
between.

Compensation per node per preserved epoch:

```
lost_in_epoch = delta_pw * bitcoin_epoch_reward(e) / total_weight(e)
```

Two columns are reported. The "observed" column uses the real
`fresh_pw - anchor_pw` and is zero for nodes with no observed fresh
follow-up. The "estimated" column uses the observed delta where available
and falls back to `anchor_pw / WeightScaleFactor - anchor_pw` (the WSF
heuristic) for nodes we never see running fresh PoC again.

`bitcoin_epoch_reward(e)` is the chain's documented decay
`initial * exp(decay_rate * (e - genesis_epoch))`, all read from
`inference.params.bitcoin_reward_params`. `total_weight(e)` is the observed
sum of `weight` across the epoch's Qwen subgroup.

Per-participant totals are then aggregated. CSVs and a summary JSON go to
`./output/`.

## CLI flags

```
--rpc URL           Public chain RPC base (default http://node2.gonka.ai:8000)
--epoch N           Anchor epoch (default 247)
--model MODEL_ID    Subgroup model id (default Qwen/Qwen3-235B-A22B-Instruct-2507-FP8)
--lookahead N       Forward epochs to scan for fresh PoC (default 12, auto-clamped to current chain epoch)
--bug-threshold F   Flag a node as observed Issue #2 victim if anchor/fresh < F (default 0.5)
--extended-cohort   Also include nodes preserved in epoch+1 but not in epoch
```

## Outputs (`./output/`)

| file | rows | description |
|---|---|---|
| `issue2_per_node.csv` | 67 | per (participant, node): anchor_pw, fresh_pw, ratio, observed and estimated GONKA |
| `issue2_per_participant.csv` | 66 | aggregated per address: observed and estimated total GONKA |
| `issue2_summary.json` | - | grand totals plus the observed-ratio histogram |
| `issue2_log.txt` | - | full RPC trace from the last run |

## Sanity check against the chain directly

Pick any victim from `issue2_per_node.csv` (here the largest single-node
loss) and watch `poc_weight` and `timeslot_allocation` flip across the
upgrade boundary:

```bash
ADDR=gonka1lr9mj6dgkv0h76c8y8w0l3esztyg9v2q8d6d8d
NODE=mnode-143

for E in 246 247 248 249 250; do
  curl -s --max-time 8 "http://node2.gonka.ai:8000/chain-api/productscience/inference/inference/epoch_group_data/${E}?model_id=Qwen%2FQwen3-235B-A22B-Instruct-2507-FP8" \
    | python3 -c "
import json, sys
d = json.load(sys.stdin)
g = d.get('epoch_group_data', {}) or {}
for vw in g.get('validation_weights') or []:
    if vw.get('member_address') != '$ADDR': continue
    for n in vw.get('ml_nodes') or []:
        if (n.get('node_id') or '') != '$NODE': continue
        ts = n.get('timeslot_allocation')
        print(f\"epoch=$E  node=$NODE  poc_weight={n.get('poc_weight'):>5}  POC_SLOT={ts[1] if len(ts)>=2 else '?'}\")
        break
    break
"
done
```

You should see `poc_weight = 2409` with `POC_SLOT = True` for epochs 247
and 248, then `poc_weight = 6761` with `POC_SLOT = False` from epoch 249
onward. That's the 2.78x jump (= 1 / WeightScaleFactor) the report is
about.

## Dependencies

Python 3.9+ stdlib only. No `pip install` required.
