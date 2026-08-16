# Foken — proof-of-contribution valuation gate

**Off-chain Half 2 of the Foken governance design. Prototype, pure
engineering — no blockchain, no tokens, no secrets, no deployment.**

This is a **coarse, deterministic pre-filter** that sits in front of the
binding governance decision. It consumes a **Foken Invoice** (a JSON
document a contributor submits), validates it against hard IP/provenance
gates, applies the **net-utility formula**, and emits a verdict:
`PASS`, `FAIL`, or `REVIEW`.

Framing: this is a **verifiable contribution pre-filter**, not equity
minting. There is no price, appreciation, or investment language here —
and there shouldn't be. Nothing in this gate mints, prices, vests, or
transfers anything.

> **The binding decision is always the human >50% vote. This formula is a
> pre-filter only — it never overrides the vote, and a PASS here does not
> mint anything.**

## Contents

- [Files](#files)
- [Run it](#run-it)
- [Foken Invoice schema](#foken-invoice-schema)
- [Validation & verdict logic](#validation--verdict-logic-in-precedence-order)
- [The net-utility formula](#the-net-utility-formula)
- [Reviewer set & quorum](#reviewer-set--quorum-the-50-rule)
- [Off-chain tally (`tally.py`)](#off-chain-tally-tallypy)
- [Pending owner params](#pending-owner-params-not-hardcoded)
- [Tests](#tests)
- [Design choices & resolved ambiguities](#design-choices--resolved-ambiguities)
- [Non-goals](#non-goals)
- [License](#license)
- [Further reading](#further-reading)

---

## Files

| File | Purpose |
|------|---------|
| `gate.py` | The CLI + gate logic, plus the reviewer-set model (`validate_reviewer_set`, `quorum_reached`) — stdlib only (`argparse`, `json`, `re`, `sys`; Python 3.11+) |
| `tally.py` | Off-chain tally / consensus service: counts distinct reviewer approvals, emits the mint-ready `mintFromConsensus` payload |
| `invoice.schema.json` | Formal JSON Schema (draft-07) for the Foken Invoice |
| `reviewers.json` | Example reviewer set (7 distinct `0x` addresses) |
| `test_gate.py` | `unittest` suite for the gate (34 tests) |
| `test_reviewers.py` | `unittest` suite for the reviewer-set model |
| `test_tally.py` | `unittest` suite for the tally service |
| `test_schema.py` | `unittest` suite for the JSON Schema (incl. a stdlib-only validator) |
| `pyproject.toml` | Minimal project metadata so `uv run` works in this dir |

## Run it

```bash
# from this directory
uv run python gate.py invoice.json          # or: python3 gate.py invoice.json
echo '{...invoice json...}' | python3 gate.py --json /dev/stdin   # stdin via path
python3 gate.py -                            # '-' also reads stdin
python3 gate.py invoice.json --history history.json   # optional median check
python3 gate.py invoice.json --human        # pretty table instead of JSON line

# reviewer set + tally
python3 tally.py --reviewers reviewers.json --votes votes.json
python3 tally.py --reviewers reviewers.json --votes votes.json \
  --review-round 2 --min-approvals 4
```

Output (default) is one JSON line:

```json
{"invoiceId": "...", "verdict": "PASS", "reason": "...", "netUtility": 82.5}
```

Exit codes: **0 = PASS**, **1 = FAIL**, **2 = REVIEW**.

`--history PATH` points at a JSON array of past invoices (or an object with
an `"invoices"` array). The **last 10** invoices in the file set the median
for the valuation-deviation check — keep the file in chronological order
(append newest last). A malformed/absent history is warned about and
skipped, never fatal.

---

## Foken Invoice schema

All fields required unless noted. The prototype validates shape, presence,
and the gated fields below; it does not recompute anything from a repo.

```json
{
  "invoiceId": "uuid-v7 string",
  "version": 1,
  "contributor": "0x-hex-address",
  "work": { "title": "", "description": "", "repo": "", "commit": "" },
  "artifact": { "provenanceHash": "sha256://...", "artifactUri": "ipfs://..." },
  "valuation": { "selfAssessedFokens": 5, "basis": "USD", "rationale": "", "evidence": ["urls"] },
  "ipAssignment": {
    "statement": "IRREVOCABLE ASSIGNMENT TO UNIVERSAL TREASURY",
    "license": "MIT-or-none",
    "signedAt": "ISO-8601",
    "signature": "0x-hex"
  },
  "netUtility": { "futureWorkAvoidedHours": 0, "workAddedHours": 0, "maintenanceDebtHours": 0 },
  "dedup": { "claimedNovelty": "", "relatedInvoiceIds": [] }
}
```

| Field | Type | Notes |
|-------|------|-------|
| `invoiceId` | string | Any non-empty string is accepted by the gate (not uuid-validated). |
| `version` | int | Currently `1`. |
| `contributor` | string | `0x` + exactly 40 hex chars. |
| `work.*` | strings | Claimed work metadata. |
| `artifact.provenanceHash` | string | Must start with `sha256://`. |
| `artifact.artifactUri` | string | e.g. `ipfs://...`. |
| `valuation.selfAssessedFokens` | int | Positive integer. Floats and booleans rejected. |
| `valuation.basis/rationale/evidence` | str / str / list | Soft fields; `evidence` is a list of URLs. |
| `ipAssignment.statement` | string | Must contain `IRREVOCABLE ASSIGNMENT`. |
| `ipAssignment.license` | string | `MIT` or none, per policy. Not gated. |
| `ipAssignment.signedAt` | string | Not date-validated (date-only strings accepted). |
| `ipAssignment.signature` | string | `0x`-prefixed hex. |
| `netUtility.*` | numbers | **Tool-computed inputs** — see below. Non-negative; may be floats. |
| `dedup.*` | str / list | Soft fields. |

### `netUtility` — tool-computed inputs

`netUtility` carries the values the **tool computes** from the claimed work
(future work avoided, work added, maintenance debt). This prototype takes
those numbers **as already-computed inputs** — it does NOT inspect the repo
or re-derive them. That computation is a separate tool (out of scope here).
The gate only checks they are present and non-negative, then applies the
formula.

### Formal JSON Schema

[`invoice.schema.json`](invoice.schema.json) is a draft-07 JSON Schema for
the Foken Invoice. It matches the field names, required-ness, and types that
`gate.py` validates, with two deliberate differences (documented in the
schema's own description):

- `ipAssignment.statement` and `ipAssignment.signature` **are** required in
  the schema, even though `gate.py` deliberately keeps them out of its
  nested-required list — an invoice without them can never pass the IP hard
  gate, so the contract requires them.
- `netUtility` is **not** required at the top level (a missing `netUtility`
  is a REVIEW in `gate.py`, never a FAIL), but when present it must carry all
  three non-negative numeric fields.

There is no third-party `jsonschema` dependency — `test_schema.py` ships a
minimal stdlib validator covering the subset the schema uses
(`type`, `required`, `properties`, `items`, `pattern`, `minLength`,
`minimum`, `exclusiveMinimum`, `enum`).

---

## Validation & verdict logic (in precedence order)

1. **Structure** — missing/type-broken required fields → **FAIL** (`malformed invoice: ...`).
2. **Hard gates** → **FAIL**:
   - `ipAssignment.signature` present and `0x`-hex, AND `statement` contains
     `IRREVOCABLE ASSIGNMENT` — else
     `missing/unsigned IP assignment — no IP, no mint`
   - `artifact.provenanceHash` present and `sha256://`-prefixed — else FAIL
     (`invalid artifact provenance: ...`)
   - `contributor` valid `0x` + 40 hex — else FAIL
     (`invalid contributor address: ...`)
   - `selfAssessedFokens` positive integer — else FAIL
     (`selfAssessedFokens must be a positive integer`)
3. **`netUtility` present & non-negative** — else **REVIEW**
   (a human can fix the numbers; the gate cannot run without them).
4. **Net-utility formula** (see below): `net <= 0` → **FAIL**; this FAIL
   wins even if the valuation also deviates — a rejected invoice does not
   go to an extra review round.
5. **Valuation deviation** (only if `--history` given):
   `selfAssessedFokens > 2 × median(last 10 history invoices)` → **REVIEW**
   (`valuation deviates >2x median; extra review round`).
6. **`net > 0`** → **PASS** ("eligible for human vote").

So: **FAIL > REVIEW > PASS**, with net-negative FAIL taking priority over
valuation REVIEW.

## The net-utility formula

```
net = futureWorkAvoidedHours − (workAddedHours + 1.5 × maintenanceDebtHours)
```

- `net > 0`  → **PASS** — eligible for the human vote.
- `net <= 0` → **FAIL** — *"net-negative utility: future work avoided is
  eclipsed by work added + maintenance debt"*.

The `1.5` weight reflects that maintenance debt is worse than work added:
it recurs.

Working example (also the README-facing smoke test):

| input | value |
|-------|-------|
| `futureWorkAvoidedHours` | 100 |
| `workAddedHours` | 10 |
| `maintenanceDebtHours` | 5 |
| **net** | 100 − (10 + 7.5) = **82.5** → PASS |

Counter-example ("moving rocks"): `0 − (40 + 1.5×60) = −130` → FAIL.

---

## Reviewer set & quorum (the >50% rule)

The reviewer-set model lives in `gate.py` and is consumed by `tally.py`:

- **Reviewer set** — a list of ≥ 5 distinct `0x` + 40-hex addresses.
  `validate_reviewer_set(reviewers, min_reviewers=5)` returns an error string
  when the set is not a list, is too small, contains a malformed address, or
  contains duplicates. `load_reviewers(path)` reads a JSON array of addresses
  (or an object with a `"reviewers"` array; `-` reads stdin) — the same
  lenient shape convention as `--history`.
- **Quorum** — `quorum_reached(approvals, set_size)` returns
  `approvals * 2 > set_size`: consensus holds when **strictly more than half**
  of the reviewer set approves. 3 of 6 is exactly 50% and does **not** quorum.
  Both spellings are available — the Pythonic `quorum_reached` and the
  spec-fidelity alias `quorumReached`.
- **`reviewers.json`** is an example set of 7 addresses (prototype; replace
  with real reviewers before use).

The spec's **k = floor(n/2) + 1** distinct approvals is exactly the smallest
count that satisfies the >50% rule (for n = 5: k = 3; n = 6: k = 4;
n = 7: k = 4), so `tally.py` uses the same threshold.

## Off-chain tally (`tally.py`)

After the gate pre-filters an invoice and the human vote collects approvals,
`tally.py` counts **distinct** approving reviewers and — when k is met —
emits the mint-ready payload matching the on-chain
`AttestationVerifier.mintFromConsensus` input:

```bash
python3 tally.py --reviewers reviewers.json --votes votes.json
```

`votes.json` is a JSON array of:

```json
[
  {"invoice": { "...foken invoice..." },
   "reviewerAddress": "0x...", "signature": "0x-hex"}
]
```

Consensus output (exit **0**) — the payload shape, exactly:

```json
{"invoiceId": "uuid-v7 string", "recipient": "0x...", "amount": 5,
 "reviewRound": 1, "approvalCount": 3, "reviewerSetSize": 5}
```

No-consensus output (exit **1**):

```json
{"consensus": false, "reason": "consensus not reached: 2/3 approvals needed (reviewer set size 5)"}
```

Behavior:

- **Signatures are format-checked only** (`0x` + hex, any length) — real
  Ed25519/secp256k1 verification is out of scope for this prototype.
- **Strict by design**: any malformed vote (bad address, a reviewer outside
  the set, a bad signature, a malformed invoice) aborts the tally — votes are
  never silently dropped. Duplicate approvals from the same reviewer count
  once. Votes must all reference the same `invoiceId`.
- `recipient` = `invoice.contributor`; `amount` = `invoice.valuation.
  selfAssessedFokens` (validated with the same rules as `gate.py`).
- **`--review-round N`** — 1 for a normal vote (default), **2 when the gate
  verdict was REVIEW** (the extra review round).
- **`--min-approvals K`** — overrides the derived k (see "Pending owner
  params" below). `-` reads stdin for either file flag.

---

## Pending owner params (not hardcoded)

The following are owner decisions that are deliberately **not** baked in;
each is a CLI arg / config with a sensible default, documented here so the
owner can flip them without code changes:

| Param | Where | Default | Notes |
|-------|-------|---------|-------|
| Reviewer count (k) | `tally.py --min-approvals` | `floor(n/2)+1` of the reviewer set size | Strict >50% per spec; overridable per run |
| Minimum reviewer-set size | `gate.validate_reviewer_set(min_reviewers=...)` | `5` | Spec-mandated ≥ 5; a per-call parameter |
| "Inactive" window (reviewer liveness) | — | not implemented | No timing logic in this prototype; would land as a `--inactive-window`-style config when decided |
| Pool split (treasury allocation) | — | not implemented | Out of scope: this prototype never allocates anything; would be config, not code |
| Gate tooling (netUtility computation) | — | not implemented | The tool that computes `netUtility` from a repo is a separate tool (out of scope); the gate takes its values as inputs |

---

## Tests

```bash
python3 -m unittest discover foken -v     # from the workspace root
python3 -m unittest discover -v           # from this directory
```

Test suites:

- `test_gate.py` — 34 tests: PASS (positive net), FAIL net-negative
  ("moving rocks", net = −130), FAIL missing IP assignment, FAIL invalid
  contributor, FAIL non-`sha256://` provenance, FAIL non-positive
  `selfAssessedFokens`, REVIEW missing/negative `netUtility`, REVIEW
  valuation >2× median (incl. the "only the last 10 history invoices count"
  rule), verdict precedence, and CLI exit codes 0/1/2 (file, `-`, and
  `--json` stdin paths, `--human`).
- `test_reviewers.py` — reviewer-set validation (size, address format,
  duplicates, overrides), the >50% quorum rule incl. the 3-of-6 boundary,
  and `load_reviewers` file/stdin shapes.
- `test_tally.py` — the k formula (and its identity with the quorum
  threshold), payload shape/values, strict-vote handling, dedup, review
  round / min-approvals overrides, and CLI exit codes 0/1/2.
- `test_schema.py` — schema/draft-07 structural checks against `gate.py`'s
  constants, plus a stdlib-only draft-07-subset validator over valid and
  invalid invoices.

---

## Design choices & resolved ambiguities

- **`netUtility` is input, not computed.** The tool-computed values are
  accepted as given; repo analysis is a separate tool, out of scope.
- **Verdict precedence** (unstated in the brief): a net-negative invoice is
  FAIL even when its valuation deviates >2× median — an already-rejected
  invoice doesn't get an "extra review round". Documented above.
- **`net == 0` is FAIL** (spec says `net <= 0` → FAIL).
- **"Last N invoices"** = tail of the history array; file order is
  chronological (newest last). Median of an even count is the mean of the
  two middle values.
- **`signature` only requires `0x`-hex** (any length) — the spec's example
  is a short hash, so no fixed length is enforced.
- **`invoiceId` is not uuid-validated** — any non-empty string passes
  (keeps the gate usable with synthetic/smoke ids like `"u"`).
- **`signedAt` is not date-validated** — date-only strings are accepted.
- **`--json PATH`** is a path carrier (the brief's invocation is
  `--json /dev/stdin`), not an output-mode flag; output is JSON by default.
- **History problems are non-fatal** (warn + skip the deviation check).

## Non-goals

- No blockchain, no token economics, no pricing, no vesting, no minting.
- No secret material anywhere — this gate reads only public invoice data.
- Not deployed, not git-homed (the owner does that separately).

## License

MIT — see [LICENSE](LICENSE). The invoice's `ipAssignment.license` field
follows the same policy (`MIT-or-none`): contributors assign their IP
irrevocably, and the tooling that judges them stays free.

## Further reading

- **The Foken governance design** — the on-chain half (AttestationVerifier,
  mintFromConsensus) this prototype pairs with.
- David Edgerton, *The Shock of the Old: Technology and Global History since
  1900* (2006) — use-centric value vs. innovation-centric attention: the
  `maintenanceDebtHours` term is exactly the use-centric lens.
- Elinor Ostrom, *Governing the Commons* (1990) — collective governance of
  shared resources: why the reviewer quorum, not the formula, is the binding
  decision.
- Vitalik Buterin, Zoë Hitzig, E. Glen Weyl, *A Flexible Design for Funding
  Public Goods* (2019) — quadratic funding as the boundary case this gate
  deliberately stops short of.
- Mike Masnick, *Protocols, Not Platforms* (2019) — why a pre-filter plus a
  human vote, with no platform in the middle.
- *Recency as Salience* and *A Mező és a Tömeg* (Vének Tanácsa, 2026) — the
  proxy critique this gate is built against: a scalar proxy turns pathological
  when a relational system delegates its continuation to it. Foken's refusal
  to price anything is the same refusal, at the governance layer.
  ([Sovereign Library](https://pocoo.vaked.dev/demos/book/))
