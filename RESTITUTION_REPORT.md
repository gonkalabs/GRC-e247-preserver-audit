# Issue #2 - Stuck-pw "0.35x" Audit (revised)

Prepared by Mike @ GonkaLabs, for the Gonka Restitution Committee.

Task wording: *"MLNodes which were sampled as preserver nodes in epoch 247
(and might be next epoch until first PoC) causes lowered weight for this
node (0.35[%/x] of the full weight)."*

Subgroup audited: `Qwen/Qwen3-235B-A22B-Instruct-2507-FP8` (the only active
multi-model subgroup at the time). Source of truth: public chain RPC at
`http://node2.gonka.ai:8000/chain-api/...`. Every number here is regenerated
end-to-end by `issue2_audit.py` (~30 s, stdlib-only).

## What changed in this version

This report has been updated after GRC review.

- The first script anchored too hard on epoch 247 preservation and missed
nodes that stayed stuck after the upgrade. That is now fixed.
- The first compensation formula used the Qwen subgroup denominator. GRC
 pointed out that the chain distributes fixed rewards against the aggregate
 epoch group total. The script now uses `root_total_weight`, defined here as
 parent `EpochGroupData.total_weight` from `epoch_group_data/{epoch}` where
 `model_id == ""`.
- The GRC chose the broad policy: affected nodes stay in the restitution set
even if the participant had misses, invalidation, or zero actual payout.
- The output CSV now includes the denominator mode, per-epoch denominator,
epoch reward, and per-epoch loss. CSV column names are kept stable; the
legacy `epoch_total_confirmation_weights` column now contains root totals
when `denominator_mode = root_total_weight`.

## Summary

The "0.35x" figure is the Qwen `WeightScaleFactor` (WSF) of **0.3593**.

When a node carried its `MLNodeInfo.PocWeight` across the v0.2.12 upgrade
without running fresh PoC under the new code, the chain re-interpreted the
already-scaled pre-v0.2.12 value as if it were raw nonces and applied WSF
again at consensus aggregation. From epoch 249 onward, that node's
consensus contribution was `WSF * pw_stored` instead of the intended
`pw_stored`. Every reward stream that depends on consensus weight (PoC
fixed reward, inference settlement) shrinks proportionally - that is the
"~36% of full reward" pattern operators reported in chat.

After the corrected detection: **34 (participant, node) pairs** are clear
victims. Total restitution comes to **30,318.50 GONKA** for the PoC fixed
reward only, using the aggregate/root total-weight denominator. Inference
settlement losses are real but out of scope for this script.

## 1. The bug, on chain

Pre-v0.2.12 stored `MLNodeInfo.PocWeight` in already-scaled units
(`raw_nonces * WeightScaleFactor`). Post-v0.2.12 stores it in raw nonces
and applies `WeightScaleFactor` at the validation_weight step. The
v0.2.12 migration in `inference-chain/app/upgrades/v0_2_12/upgrades.go`
clears legacy PoC v2 collections but does not rewrite existing
`MLNodeInfo.PocWeight` values, so any node that didn't run fresh PoC under
v0.2.12 stayed with a stale, pre-scaled value.

The reward formula (from `inference-chain/x/inference/keeper/bitcoin_rewards.go`,
v0.2.12) reads:

```
effectiveWeight = root-weight-scale confirmed weight
denominator     = parent EpochGroupData.total_weight
reward          = effectiveWeight * fixedEpochReward / root_total_weight
```

For a stuck node with `pw_stuck` (pre-scaled units), the chain treats this
as raw and computes `effective = WSF * pw_stuck`. The intended consensus
contribution would be `pw_stuck` (= `WSF * pw_raw` where `pw_raw = pw_stuck / WSF`).
The per-epoch shortfall in the numerator is `(1 - WSF) * pw_stuck`.

You can see this directly on chain. Pick the GRC member's case
(`gonka16k...`):


| epoch | weight | conf    | sum_pw | conf/weight | nodes                                                              |
| ----- | ------ | ------- | ------ | ----------- | ------------------------------------------------------------------ |
| 246   | 2685   | 0       | 2685   | 0.000       | node1 pw=2685 (preserved, Issue #1)                                |
| 247   | 2685   | 0       | 2685   | 0.000       | node1 pw=2685 (preserved, Issue #1)                                |
| 248   | 2685   | 2685    | 2685   | 1.000       | node1 pw=2685 (last v0.2.11 reward formula, no WSF)                |
| 249   | 2685   | **964** | 2685   | **0.359**   | node1 pw=2685 (v0.2.12 reward formula, WSF applied to stale value) |
| 250   | 2685   | 964     | 2685   | 0.359       | same                                                               |
| 251   | 13738  | 4936    | 13738  | 0.359       | node1 retired, node3+node4 fresh PoC under v0.2.12                 |


`conf/weight = 0.359` in epochs 249-250 is the chain applying WSF
universally. For node1, `weight = 2685` is the stale pre-scaled value,
so `conf = 964 = 0.3593 * 2685`. The intended consensus contribution
(if node1 had been re-measured in raw units, ~7475) would have been
`WSF * 7475 = 2685`. Difference: `(1 - WSF) * 2685 ≈ 1720`.

Same pattern verified across every node in Cohort B below.

## 2. Detection (Cohort B)

Anchor: epoch 248, the last snapshot under v0.2.11 storage convention.
For every (participant, node) seen at epoch 248 we walk epochs 249..253:

- **Stuck epoch**: `pw_E / pw_baseline` in `[0.95, 1.10]` - the value
didn't move, so the node didn't re-PoC under v0.2.12.
- **Fix epoch**: `pw_E / pw_baseline >= 2.0` - the node re-PoC'd under
v0.2.12 in raw units (typical jump is ~1/WSF ≈ 2.78x). We stop counting
stuck epochs at this point.

Anything in `(1.10, 2.0)` is ambiguous (8 nodes; see section 5) and
excluded for safety. Anything `< 0.95` is a node whose hardware actually
shrunk (small handful; not the bug we're auditing).

## 3. Compensation methodology

For each (node, stuck epoch E):

```
lost_share_E  = (pw_baseline - floor(WSF * pw_baseline)) / rootTotalWeight(E)
lost_ngonka_E = fixedEpochReward(E) * lost_share_E
```

`fixedEpochReward(E)` is the chain's own
`initial_epoch_reward * exp(decay_rate * (E - genesis_epoch))`, computed
from `inference.params.bitcoin_reward_params` (initial = 323,000 GONKA,
decay = -0.000475). At the affected epochs that's about 287,100 GONKA per
epoch. `rootTotalWeight(E)` is the parent `EpochGroupData.total_weight` from
`/chain-api/productscience/inference/inference/epoch_group_data/{E}` without
`model_id`.

This formula matches the denominator used by observed payouts. It is still a
linear approximation: it does not attempt to recompute the final settlement
table after changing all affected weights at once.

It does NOT model:

- Inference settlement payouts (also dependent on consensus weight).
- Collateral / power capping effects (none of the victims here are large
enough to be capped, but the GRC may want to verify).
- Slashing / downtime adjustments per epoch. Per GRC vote, affected nodes
remain included even if they had misses, invalidation, or zero actual payout.

Denominator note: `root_total_weight` is not a literal chain field name. It is
the name used in this audit for the aggregate/root epoch total. The API field
is `epoch_group_data.total_weight` on the parent group, where `model_id` is
empty. For epoch 249:

```
GET /chain-api/productscience/inference/inference/epoch_group_data/249
model_id = ""
total_weight = 740094
```

This resolves the `gonka1p60...` discrepancy exactly:

```
pw_base = 1053
floor(1053 * 0.3593) = 378
lost_root_weight = 1053 - 378 = 675
675 / 740094 * 287106.240501 = 261.8541865 GNK
```

## 4. Restitution table

Sorted by `total_lost_gonka` desc. Full data in
`output/issue2_per_node.csv` and `output/issue2_per_participant.csv`.


| participant                                    | node             | pw_base | stuck epochs        | fixed in window? | lost (GONKA)      |
| ---------------------------------------------- | ---------------- | ------- | ------------------- | ---------------- | ----------------- |
| `gonka1zsvl7ujlc8z3a35v2q6e3nml7ftyk23v76jqgl` | node-1           | 2760    | 249,250,251,252,253 | no               | **3,409.330900**  |
| `gonka1jltjehxsnum94nt8c00ts7khmpy4lafv6gryzk` | U7               | 1720    | 249,250,251,252     | no               | **1,741.951170**  |
| `gonka1slndy4rsmld579628302rj5gz8z9qf4v6ppmc4` | host01           | 2166    | 249,250,251         | yes (e252)       | **1,602.460126**  |
| `gonka1r5hdy9q5v783ef7td98k4c68cxl6a58h5sytfq` | node2            | 6399    | 249                 | no               | **1,590.521726**  |
| `gonka187tn9y92ur6tu0zf69u94hwl0q77m47y0k36hv` | ice-1            | 2127    | 249,250,251         | yes (e252)       | **1,573.597372**  |
| `gonka1pllyukkeymx3hfd9mts3pryr9y6efs9eshty87` | malay-an-3       | 2125    | 249,250,251         | yes (e252)       | **1,572.442861**  |
| `gonka1u9a7r4w76gult5n9ysadnual9fghkc6yda60wj` | node1            | 1511    | 249,250,251,252     | no               | **1,530.327002**  |
| `gonka14tqh62mangwzrma2lgg2dm375rcjzn2ydy8ttm` | 252-5            | 2004    | 249,250,251         | yes (e252)       | **1,482.391068**  |
| `gonka1q5xt54wncgzk7dxv9x64uln68455g83wu9tugg` | x104             | 2805    | 249,250             | yes (e251)       | **1,348.921565**  |
| `gonka1h3s37p0l23mg6ak9h9nmayh6r9f2vm6umj3qet` | node             | 2794    | 249,250             | no               | **1,343.669924**  |
| `gonka16k03ze5ynkprsd4n6e5uzhthvu9jjk553rauqy` | node1            | 2685    | 249,250             | no               | **1,291.153511**  |
| `gonka1zpw8tml8xl4fm6zm8zpf2u4pq4tehmd9e2vgq7` | rock             | 2604    | 249,250             | no               | **1,252.141319**  |
| `gonka12pcu9mcrpa4w4sjd9y3dsksnvu495ss6f9r4ra` | node2            | 2403    | 249,250             | no               | **1,155.361073**  |
| `gonka188c86f9mrlt4nlcg89f82nnfm9jzq9gtjafj50` | node5            | 3887    | 249                 | yes (e250)       | **966.338931**    |
| `gonka10mmdjau4dnj8krs7sh7t7635ttnmq9u3vqgz09` | node7            | 3108    | 249                 | yes (e250)       | **772.760799**    |
| `gonka1famtxh54kad6ylwtm60j6d7h6unpc08d4vdqnk` | I19              | 3006    | 249                 | yes (e250)       | **747.157279**    |
| `gonka1dpt9zx2dqcky6yjjwrd8xz2w7lq6vffy9mhvgs` | worker_gpu_alpha | 2549    | 249                 | no               | **633.881098**    |
| `gonka1lswsj2x7u4606wqpunmm07skgf76r3dyz4v0d8` | nv-compute-east  | 2529    | 249                 | no               | **628.837980**    |
| `gonka1ge9amk4ymld27d35akj3ky9uph4gyz6rdpepjj` | EdgeTPU-3        | 2452    | 249                 | no               | **609.441373**    |
| `gonka1llgg3kvg9sc6xz09jtkcrucrppxgn78xe4xlv0` | inference        | 2391    | 249                 | no               | **594.312020**    |
| `gonka145666cll76ptcyy9ceymtalr8gnvv73ne99p32` | inference-prod   | 2337    | 249                 | no               | **581.122328**    |
| `gonka1ccdm8j6sjyhq4qask049dwgaczs7f3pxte6zmp` | main             | 2126    | 249                 | yes (e250)       | **528.751491**    |
| `gonka1043d00lu0v3fz53cut34twtcanalqg9u8vehp2` | node1            | 2046    | 249                 | no               | **508.579020**    |
| `gonka1umvyh0rz5fdmk9qhxurshhchennajced6f4s89` | node-6           | 1933    | 249                 | no               | **480.647907**    |
| `gonka16q0zaetd6hq6d8zj48ur0v967xrrwh566kcazc` | ml-node-c087b09b | 1553    | 249                 | no               | **386.380400**    |
| `gonka1d694r00czmq75txghwjcuk07lxvc8d4ekgsha0` | mlnode-060       | 455     | 249,250,251         | no               | **337.116972**    |
| `gonka168rtjfkszuhcggg4dfyse4yh7xn9zwfglnkns2` | mlnode-001       | 448     | 249,250,251         | no               | **332.498931**    |
| `gonka1zktn8j65wlys8a8e38hqhf4y3x6m4x04zskkrx` | node_ovh         | 366     | 249,250,251         | yes (e252)       | **271.309892**    |
| `gonka1p60lruhxmwcsa9taa28cp4k4f6kv2kvyu5h5ep` | inference-prod   | 1053    | 249                 | no               | **261.854187**    |
| `gonka1y2a9p56kv044327uycmqdexl7zs82fs5ryv5le` | node-235b        | 994     | 249                 | yes (e250)       | **247.112766**    |
| `gonka1d7p03cu2y2yt3vytq9wlfm6tlz0lfhlgv9h82p` | node1            | 627     | 249                 | yes (e250)       | **155.948716**    |
| `gonka1p2lhgng7tcqju7emk989s5fpdr7k2c3ek6h26m` | node2            | 627     | 249                 | yes (e250)       | **155.948716**    |
| `gonka1wthc28t25pg63hzvl07rl8e8r6km6hesl6jhsz` | rtx-test-1       | 452     | 249                 | yes (e250)       | **112.500317**    |
| `gonka17pw6099q758qwzewtrqmqpf5c2lrhr97fwqexu` | france-rtx-2     | 449     | 249                 | yes (e250)       | **111.724453**    |
|                                                |                  |         |                     | **TOTAL**        | **30,318.495191** |


Breakdown by stuck-window length:

- 5 epochs (249-253, no fix observed): 1 node, 3,409 GONKA
- 4 epochs: 2 nodes, 3,272 GONKA
- 3 epochs: 7 nodes, 7,172 GONKA
- 2 epochs: 5 nodes, 6,391 GONKA
- 1 epoch: 19 nodes, 10,074 GONKA

19 nodes self-resolved in epoch 250 because the v0.2.12 episode-scoped
preservation (PR #1089) gave them a fresh PoC slot. The 15 that stayed
stuck longer kept getting sampled into preservation; some were still
stuck at epoch 253 when this audit was last run.

## 5. Borderline cases (excluded)

Eight (participant, node) pairs sit in the ambiguous 1.10-2.00 ratio band:


| participant                                    | node        | pw_base | first jump | ratio |
| ---------------------------------------------- | ----------- | ------- | ---------- | ----- |
| `gonka12av9up884t9lcsf70rs0l7jfmkmc8k9sxfuknt` | FT-23       | 3611    | e249, 4689 | 1.299 |
| `gonka125n6kr5gvdup0lndfkps7t6rd6592panhrg3np` | node481     | 3102    | e249, 3848 | 1.240 |
| `gonka1wthc28t25pg63hzvl07rl8e8r6km6hesl6jhsz` | ml-tango-01 | 2962    | e249, 4077 | 1.376 |
| `gonka1zktn8j65wlys8a8e38hqhf4y3x6m4x04zskkrx` | nvh100_5    | 2406    | e253, 4334 | 1.801 |
| `gonka1pllyukkeymx3hfd9mts3pryr9y6efs9eshty87` | malay-an-1  | 2216    | e253, 3428 | 1.547 |
| `gonka1fkrsesmn2hdj30fhwyam6h4f2e77un36xalhvl` | malay-az-2  | 2061    | e249, 3043 | 1.476 |
| `gonka1tlvg4kjx7ljd5thgd5fkgh39q6lu8cmxupktgg` | node1       | 1988    | e250, 2923 | 1.470 |
| `gonka1tmk2tzdneht6smu34pkmqdvu7p34qavvmwtwq2` | node1       | 1540    | e249, 2964 | 1.925 |


These could be partial-PoC, hardware change, or driver downgrade rather
than the migration bug. The ratio is too far from 1.0 to call them stuck
and too far from 2.78 to call them properly re-PoC'd. The GRC may want to
ask each operator before deciding. None of them appear in the totals
above.

## 6. Upstream patch reference

`gonka-ai/gonka` PR #1089 ("Random selection of preserved MLNodes",
shipped in `release/v0.2.12`). Two pieces fix this:

1. Episode-scoped preservation, so every PoC anchor materializes a fresh
  preserved snapshot. Nodes that stay sampled into preservation across
   multiple anchors keep their stale `MLNodeInfo.PocWeight`; the 15 nodes
   in the table that self-resolved at e250 are the ones that
   weren't sampled again.
2. Reward weight calculation refactor in `bitcoin_rewards.go` that uses
  `ConfirmationWeight` (now WSF-applied uniformly) as numerator with
   capping against `vw.Weight`.

The migration handler `clearLegacyPoCv2Data` did not rewrite existing
`MLNodeInfo.PocWeight` values, which is the root cause of the stuck-pw
condition. A targeted backfill (multiply every preserved-node `PocWeight`
by `1/WSF` once at the v0.2.12 boundary) would have prevented this.

## 7. Caveats

What the report claims:

- 34 (participant, node) pairs had `MLNodeInfo.PocWeight` stored at the
pre-v0.2.12 scaled value across one or more post-upgrade epochs. This is
verifiable directly against the chain by reading
`epoch_group_data/{epoch}?model_id=Qwen/...` for each address and node.
- For each such node, the consensus contribution at the affected epoch was
`WSF * pw_stuck` instead of the intended `pw_stuck`. The 0.359 ratio of
`confirmation_weight / weight` at those addresses confirms this directly.
- The compensation amounts are the linear-approximation PoC reward delta,
computed from the chain's own `BitcoinRewardParams` and the chain's own
aggregate/root `EpochGroupData.total_weight` per epoch.

What the report does NOT claim:

- That this is the only damage. Inference settlement payouts also depend
on consensus weight; a stuck node loses ~64% of its inference share for
every stuck epoch as well. That delta is harder to back-compute from
public data and is not included in `lost_gonka` here.
- That the borderline cohort (section 5) is or isn't affected. They need
per-operator review.
- That this captures other migration issues. Item #1 from the GRC list
(POC_SLOT=true => confirmation_weight=0) is a separate, chronic
pre-v0.2.12 design covered by the same upstream PR but not by this
audit.

## 8. Files in this folder

```
scripts/epoch247_preserver_audit/
├── issue2_audit.py                    the audit script (stdlib-only, ~30 s)
├── README.md                          how to re-run
├── RESTITUTION_REPORT.md              this document
└── output/
    ├── issue2_per_node.csv            per-(participant, node) restitution
    ├── issue2_per_participant.csv     aggregated per address
    ├── issue2_summary.json            totals + cohort sizes
    └── issue2_log.txt                 full RPC trace
```

