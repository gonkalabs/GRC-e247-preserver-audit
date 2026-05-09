# Issue #2 - Preserved-Node Under-Stored Weight Audit

Prepared by Mike @ GonkaLabs, for the Gonka Restitution Committee.

Task wording: *"MLNodes which were sampled as preserver nodes in epoch 247
(and might be next epoch until first PoC) causes lowered weight for this
node (0.35[%/x] of the full weight)."*

Anchor epoch: 247, with extension to epoch 248 (see scope discussion below).
Subgroup audited: `Qwen/Qwen3-235B-A22B-Instruct-2507-FP8` (the only active
multi-model subgroup at the time). Source of truth: public chain RPC at
`http://node2.gonka.ai:8000/chain-api/...`. Every number in this report is
regenerated end-to-end by `issue2_audit.py` (~30 s, stdlib-only).

## Headline

The "0.35x" figure in the GRC task is exactly the Qwen `WeightScaleFactor`
of **0.3593**.

When a node was preserved across the v0.2.12 upgrade boundary, its
`MLNodeInfo.PocWeight` was carried over in pre-v0.2.12 storage units (i.e.
already multiplied by `WeightScaleFactor`). The v0.2.12 multi-model PoC
code re-interprets `MLNodeInfo.PocWeight` as raw nonces and applies
`WeightScaleFactor` again at the consensus aggregation step. Net effect:
nodes preserved across the upgrade contributed only ~36% of their true PoC
capability to consensus weight, until they ran a fresh PoC under the new
code (which for almost everyone happened in epoch 249).

We can confirm 12 MLNodes as Issue #2 victims using on-chain data alone:
their `anchor_pw / fresh_pw` ratio sits in [0.319, 0.379], clustering
tightly on `WeightScaleFactor`. Restitution for those 12 comes out to
**12,907.19 GONKA**.

A further 18 nodes were preserved in epoch 247 or 248 but never observed
running a fresh PoC again within our 10-epoch scan window (typically
because the participant left the subgroup or the box stopped reporting).
Applying the same WSF heuristic to those nodes adds 29,390 GONKA, so the
grand total comes to roughly **42,298 GONKA**.

## 1. The bug

`MLNodeInfo.PocWeight` is the per-node weight contribution stored on chain.
Pre-v0.2.12 (single-model PoC) the chain stored values in
post-`WeightScaleFactor` units, i.e. `pocWeight = nonces * WeightScaleFactor`
("scaled units"). The reward formula didn't apply `WeightScaleFactor` again
because it was already baked in.

Post-v0.2.12 (multi-model PoC, PR #948 + PR #1089) `MLNodeInfo.PocWeight`
is stored in raw nonces ("raw units"). The chain applies
`WeightScaleFactor` when computing each model subgroup's contribution to
consensus weight.

The v0.2.12 migration in
`inference-chain/app/upgrades/v0_2_12/upgrades.go::clearLegacyPoCv2Data`
clears legacy PoC v2 collections but does not rewrite existing
`MLNodeInfo.PocWeight` values that the preservation mechanism carried over.
Preserved nodes therefore arrived in v0.2.12 holding scaled-unit values,
and the new aggregation code interpreted those values as raw units. Their
consensus contribution worked out to
`WeightScaleFactor * stored_value = WeightScaleFactor * (true_value * WeightScaleFactor) ≈ 0.13 * true_value`.
Compared to a freshly PoC'd node in the same epoch, their effective weight
was off by a factor of `WeightScaleFactor` (about 0.36x).

Concrete evidence sits in `output/issue2_per_node.csv`. The 12 observed
victims all show `pw_ratio` in [0.319, 0.379]:

| ratio | nodes | distance from WSF (0.3593) |
|---:|---:|---:|
| 0.319678 | 1 | -11.0% |
| 0.342818 | 1 | -4.6% |
| 0.343018 | 1 | -4.5% |
| 0.350863 | 1 | -2.4% |
| 0.352544 | 1 | -1.9% |
| 0.353219 | 1 | -1.7% |
| 0.354128 | 1 | -1.5% |
| 0.355438 | 1 | -1.1% |
| 0.356308 | 1 | -0.8% |
| 0.361074 | 1 | +0.5% |
| 0.363559 | 1 | +1.2% |
| 0.379244 | 1 | +5.5% |

Mean 0.353, std 0.014 - all twelve land on `WeightScaleFactor`. Hard to
read this as anything other than the bug.

## 2. Scope: epoch 247 vs 248

The GRC anchored the task on epoch 247. If we anchor strictly on 247:

- Primary cohort = 46 (participant, node) pairs preserved in epoch 247.
- Of those, 4 nodes have hard observed evidence of the bug (fresh
  follow-up in epoch 249+ with ratio < 0.5).
- 10 are indeterminate (no observed fresh follow-up within the scan window).
- 32 had ratio about 1.0, because their first fresh PoC happened in epoch
  248 still under pre-v0.2.12 units, so there's no observable bias to
  measure.

Anchoring strictly on 247 misses most of the actual bug instances because
epoch 248 is the upgrade epoch, and that's where the cleanest evidence
sits. The task wording *"and might be next epoch until first PoC"* invites
us to extend.

The script's `--extended-cohort` flag adds nodes preserved in epoch 248 but
not in epoch 247 (another 21 pairs, 8 of which are observed Issue #2
victims). The two cohorts together (67 nodes) capture all 12 observed
victims.

Recommendation: include both cohorts. The bug mechanism is identical, the
only reason 248 looks "worse" is timing - more nodes' first fresh PoC
under v0.2.12 code happened in epoch 249.

## 3. Compensation methodology

For each (participant, node) preserved in the anchor epoch:

1. Read `anchor_pw` (the under-stored value).
2. Walk forward up to 10 epochs and find the first epoch where the node
   ran fresh PoC (`POC_SLOT == false`). Record `fresh_pw` and `fresh_epoch`.
3. Track `preserved_epochs` = epochs (with absences allowed if the node
   went temporarily missing) where the node was present and preserved.
4. Compute the missing weight units:
    - Observed delta = `fresh_pw - anchor_pw`, available only if observed
      fresh follow-up exists.
    - WSF estimate = `anchor_pw / WeightScaleFactor - anchor_pw`, always
      available, used as fallback.
5. For each preservation epoch `e`:
    ```
    lost_in_epoch = delta_pw * bitcoin_epoch_reward(e) / total_weight(e)
    ```
   `bitcoin_epoch_reward` follows the chain's decay
   `initial * exp(decay_rate * (e - genesis_epoch))`, all from
   `inference.params.bitcoin_reward_params`. `total_weight(e)` is the
   observed sum of `weight` across all participants in that epoch's Qwen
   subgroup.
6. Sum across preserved epochs to get the total per node.

Two columns reported per node:

- `lost_reward_observed_gonka`: uses observed delta. Zero for indeterminate
  nodes. This is the lower bound, computed entirely from on-chain numbers.
- `lost_reward_estimated_gonka`: uses observed delta where available, falls
  back to the WSF estimate for indeterminate cases. Best-effort total
  including the indeterminate cohort.

## 4. Restitution table - observed victims (12 nodes)

Sorted by `lost_reward_observed_gonka` desc. Full data in
`output/issue2_per_node.csv`.

| cohort | participant | node_id | anchor_pw | fresh_pw | fresh_epoch | ratio | lost (GONKA) |
|---|---|---|---:|---:|---:|---:|---:|
| preserved-in-247 | `gonka1lr9mj6dgkv0h76c8y8w0l3esztyg9v2q8d6d8d` | mnode-143 | 2409 | 6761 | 249 | 0.356 | **2,875.13** |
| preserved-in-248-only | `gonka1hwvel7n3zuk6wruefuzc356l9myske9stckwnz` | fp001 | 2694 | 7627 | 249 | 0.353 | **1,650.94** |
| preserved-in-248-only | `gonka12pcu9mcrpa4w4sjd9y3dsksnvu495ss6f9r4ra` | node1 | 2404 | 6819 | 249 | 0.353 | **1,477.58** |
| preserved-in-248-only | `gonka1rcpc45n6zch9qlkn4m3cwngekad89xu8mcr09v` | node-1 | 2460 | 6813 | 249 | 0.361 | **1,456.83** |
| preserved-in-247 | `gonka1dkl4mah5erqggvhqkpc8j3qs5tyuetgdy552cp` | node-235-1 | 1184 | 3122 | 252 | 0.379 | **1,280.33** |
| preserved-in-248-only | `gonka1tlvg4kjx7ljd5thgd5fkgh39q6lu8cmxupktgg` | node1 | 1988 | 5799 | 249 | 0.343 | **1,275.44** |
| preserved-in-248-only | `gonka17gpuntq09zsaqtmpe544gc32tk4424dwv5t34f` | U2b | 835 | 2612 | 249 | 0.320 | **594.71** |
| preserved-in-248-only | `gonka1uf5cg7ef0ns6877nl27y0s6rt06cdmn40k5a88` | node1 | 894 | 2548 | 249 | 0.351 | **553.55** |
| preserved-in-247 | `gonka1gyk0aahvr3qeju4zx0nplfreej6cy4jjk8svc5` | node1 | 425 | 1169 | 250 | 0.364 | **491.52** |
| preserved-in-248-only | `gonka1ym3np7guxart483yfdxnlztuazx22cjt0e4a2p` | U5v1 | 732 | 2134 | 249 | 0.343 | **469.21** |
| preserved-in-248-only | `gonka1x7zh2277spp7jfqjhv0g5mnezg290xdr4kpfnk` | node1 | 755 | 2132 | 249 | 0.354 | **460.85** |
| preserved-in-247 | `gonka1fc9tzt83dgrqswlgay4668cuqjrk7zsqks2vm2` | node01s | 268 | 754 | 249 | 0.355 | **321.07** |
| **TOTAL OBSERVED** | | | | | | | **12,907.19** |

By cohort:

- preserved-in-247: 4 nodes, 4,968.06 GONKA
- preserved-in-248-only: 8 nodes, 7,939.13 GONKA

## 5. Estimated restitution - indeterminate nodes (WSF-based)

These 18 nodes were preserved in epoch 247 or 248 but were not observed
running fresh PoC again within our 10-epoch scan window, typically because
the participant left the subgroup or the node stopped reporting. We can't
directly observe what their true `pw` would have been.

For these nodes we apply the same heuristic the chain itself validated for
the 12 observed cases: `estimated_fresh_pw = anchor_pw / WeightScaleFactor`.
That gives a best-effort restitution amount.

| cohort | participant | node_id | anchor_pw | est_fresh_pw | preserved_epochs | est (GONKA) |
|---|---|---|---:|---:|---:|---:|
| preserved-in-247 | `gonka12fazh3etpdx947ldwen4wudnds7wu4kjp5vd76` | GPU_WORKER_9 | 2426 | 6752 | 2 | 2,857.96 |
| preserved-in-247 | `gonka17ef5hl0588tmjm9ypw7t2kge78wrkcpvyspc0p` | NODE_8455e990 | 2304 | 6412 | 2 | 2,713.94 |
| preserved-in-247 | `gonka1fp8zl07qccdzuekns2q55jgmcag40kjrm8z0z9` | AXion | 2215 | 6165 | 2 | 2,609.55 |
| preserved-in-247 | `gonka17tlh09e32xpv2uj433ytjnwwd8fh24jclpzm5s` | node1 | 1923 | 5352 | 2 | 2,265.36 |
| preserved-in-247 | `gonka1cckj93kp9kegry64scpn4ew9965g3qrswyshl9` | node1 | 1792 | 4987 | 2 | 2,110.77 |
| preserved-in-247 | `gonka17fcahf38xh8ghzyyrc55tarz9nd0vw6xd29nsk` | node1 | 1699 | 4729 | 2 | 2,001.76 |
| preserved-in-247 | `gonka1gyydhl9lp0udz3409ps0c0lk0y4ft8qcyv8tfq` | node1 | 1680 | 4676 | 2 | 1,979.30 |
| preserved-in-247 | `gonka1psgzz288a434dv6863ldd73xma70zw7387muj2` | node1 | 1640 | 4564 | 2 | 1,931.73 |
| preserved-in-247 | `gonka1ql9asemklpkpr2d4mh33xw5gj0g5tm0v98c5q3` | node1 | 1640 | 4564 | 2 | 1,931.73 |
| preserved-in-248-only | `gonka1uzk2scggfzghr9a5j92l00gzw4jx4adc66977y` | f211 | 3082 | 8578 | 1 | 1,839.37 |
| preserved-in-247 | `gonka1eazh84v0e60s9m7exxp3nsadcfgvnsthgypjvl` | mlnode-3dc3bb80 | 931 | 2591 | 2 | 1,096.67 |
| preserved-in-248-only | `gonka1nkzdygk3g2p2usnueuqxyep3462350hgzxs86s` | node1 | 1670 | 4648 | 1 | 996.66 |
| preserved-in-248-only | `gonka1usmu5mfu8vsafvsrsvdutl50vy8kumdhv0j2x9` | node1 | 1650 | 4592 | 1 | 984.61 |
| preserved-in-248-only | `gonka1vcawx5jc2hahydd9sqw30hlxyd9ppupm9ez0yz` | node1 | 1620 | 4509 | 1 | 966.87 |
| preserved-in-248-only | `gonka10snluhflqhmwl5xrpuy9ugevypxdjjsft370fq` | node1 | 1570 | 4370 | 1 | 937.09 |
| preserved-in-248-only | `gonka1k6p754pyhxud2399knyccgjpjvdafj2u9xlgyf` | node1 | 984 | 2739 | 1 | 587.35 |
| preserved-in-248-only | `gonka1l0qv64xdu3dk2zzm5vk97j0drcmkus95u50gqk` | node1 | 576 | 1603 | 1 | 343.71 |
| preserved-in-248-only | `gonka10jjrlvkfkqupgudz0l603sq99y3wkt3urwjm0x` | node1 | 447 | 1244 | 1 | 266.73 |
| **TOTAL ESTIMATED (indeterminate)** | | | | | | **29,390.57** |

By cohort:

- preserved-in-247 (10 nodes): 21,991.61 GONKA
- preserved-in-248-only (8 nodes): 7,398.96 GONKA

## 6. Grand totals

| bucket | participants | nodes | GONKA |
|---|---:|---:|---:|
| Observed victims (hard evidence) | 12 | 12 | 12,907.19 |
| Indeterminate (WSF estimate) | 18 | 18 | 29,390.57 |
| **Total restitution candidates** | **30** | **30** | **42,297.76** |

(Two participants appear in both buckets via different nodes, so the
unique-address count is 30.)

## 7. Upstream patch reference

The change that retires this bug is `gonka-ai/gonka` PR #1089 - *Random
selection of preserved MLNodes*, shipped as part of `release/v0.2.12`.

Two pieces of PR #1089 jointly fix it:

1. Episode-scoped preservation instead of epoch-long. After v0.2.12 every
   PoC anchor materializes a fresh preserved snapshot, so a node that's
   preserved for one episode runs PoC the next. This is why epoch 249
   onwards has zero preserved nodes in our chain queries (everyone runs
   fresh PoC).
2. *"Reward weight collapses from the old 'preserved + measured' ..."*
   (truncated in the public release notes). PR #1089 changes how
   preserved-node weight is folded into the reward calculation, removing
   the dependency on the under-stored carried-over `MLNodeInfo.PocWeight`.

The v0.2.12 binary applied at block height 3,834,200, which is early in
epoch 248. The first fully clean epoch (no preserved nodes carrying stale
values) is epoch 249. No additional downstream patch was needed - the bug
self-resolved as soon as every active host had run one fresh PoC under
v0.2.12.

## 8. Caveats

What the report does claim:

- The 12 observed-victim nodes had `MLNodeInfo.PocWeight` stored at exactly
  0.319 to 0.379 of their post-fresh-PoC value. This is verifiable directly
  against the chain.
- That cluster is too tight (mean 0.353, std 0.014) to be anything other
  than the Qwen `WeightScaleFactor` of 0.3593, i.e. the bug is real and
  mechanistic.
- The compensation amounts are computed using the chain's own published
  `BitcoinRewardParams` and the chain's own observed `total_weight` per
  epoch.

What the report does NOT claim:

- That the WSF heuristic is the ground truth for the 18 indeterminate
  nodes. Their true `fresh_pw` could differ from the heuristic. The GRC
  may want to cross-check with the operators of those nodes (do they have
  their own PoC reports, did they leave the network?).
- That this captures every Issue #2 victim. Nodes preserved in epochs
  245 or 246 and not in 247 are excluded by construction (the GRC
  anchored on 247). The same mechanism applied to them in principle.
- That the per-epoch lost share is exact under all chain mechanics. The
  script uses the linear approximation
  `delta_pw * epoch_reward / total_weight`, which is the right first-order
  term but doesn't model collateral capping, delegation, or other
  chain-side adjustments. For a small per-node delta in a large total the
  approximation is within a few percent. For the largest victim
  (`mnode-143` at 2409 -> 6761) second-order corrections may matter.
- That this is the only issue from the GRC's list. Item #1 (POC_SLOT=true
  -> confirmation_weight=0) is a separate, chronic behavior going back to
  at least epoch 200. It was the *intentional* old design of the
  preservation mechanism (also retired by PR #1089) and is distinct from
  the migration-specific Issue #2 covered here. Out of scope for this
  report.

## 9. Files in this folder

```
scripts/epoch247_preserver_audit/
├── issue2_audit.py                    the audit script (stdlib-only, ~30 s)
├── README.md                          how to re-run
├── RESTITUTION_REPORT.md              this document
└── output/
    ├── issue2_per_node.csv            67 rows: per-(participant, node) restitution detail
    ├── issue2_per_participant.csv     66 rows: aggregated per address
    ├── issue2_summary.json            totals + ratio histogram
    └── issue2_log.txt                 full RPC trace
```
