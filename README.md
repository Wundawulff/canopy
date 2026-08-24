# ANIMI Chain

A registry and ranking layer for AI-generated images and short video, built as a
Canopy appchain on the Python plugin.

The chain stores **proof, not payload**. A work is identified by the SHA-256 hash
of its source file; the file itself lives off-chain behind an IPFS or HTTPS
pointer and never enters chain state.

---

## Why this exists

Ranking systems for generated content have a structural problem: if an
endorsement is free, it is free to manufacture. One script can hold thousands of
addresses, and a ranking that costs nothing to move is not a signal.

ANIMI prices the endorsement instead. Every vote carries a transfer that settles
into the creator's account. Manufacturing a ranking therefore requires a real
balance, and the spend lands with the author rather than being burned.

---

## Design

| | |
|---|---|
| Base | Canopy Python plugin (`plugin/python`) |
| Custom transactions | `submit`, `vote` |
| Custom state prefixes | `100` (Submission), `101` (Vote) |
| Media storage | Off-chain, referenced by hash + URI |

### `submit`

Registers a work. Validated statelessly in `check_tx`:

- `creator_address` must be 20 bytes
- `content_hash` must be exactly 32 bytes (a real SHA-256)
- `media_uri` must be non-empty, at most 256 chars, and start with `ipfs://` or `https://`
- `media_type` must be `0` (image) or `1` (video)
- `title` must be non-empty and at most 100 chars

In `deliver_tx`, a `content_hash` that already exists is rejected, so the same
file cannot be registered twice.

### `vote`

Records an endorsement and transfers the attached amount to the creator.
Enforced in `deliver_tx`:

- the referenced submission must exist
- the `(content_hash, voter_address)` record must not already exist — one address, one vote per work
- the creator cannot vote on their own entry
- the voter's balance must cover `amount + fee`
- every accumulation is checked against `UINT64_MAX` before it is applied

Double-voting is prevented structurally rather than by a counter: the state key
`prefix(101) || content_hash || voter_address` either exists or it does not.

---

## State layout

Canopy reserves single-byte prefixes `1-15` for core state and panics at
handshake if a plugin declares a colliding prefix. ANIMI therefore uses `100`
and `101`, both declared in `CONTRACT_CONFIG["custom_state_prefixes"]`.

```
100 || content_hash                    -> Submission
101 || content_hash || voter_address   -> Vote
```

`Submission` carries the creator address, media pointer, media type, title,
creation height, vote count and cumulative amount received.

---

## Build and test

```bash
cd plugin/python
make proto          # regenerate protobuf bindings after editing tx.proto
python -m pytest tests/test_animi.py -q
```

`tests/test_animi.py` runs the contract against an in-memory state store and
covers the validation rules, the duplicate guard, the double-vote guard, the
self-vote guard, balance accounting and fee-pool accounting.

> Note: `tests/test_contract.py` from the upstream template fails to import
> unless the tutorial message types are also generated. That failure predates
> this fork.

---

## Layout

```
plugin/python/
  contract/
    contract.py            # transaction handlers and state keys
    proto/tx.proto         # MessageSubmit, MessageVote, Submission, Vote
  tests/test_animi.py      # contract tests
```

---

## Status

Testnet. Parameters, fees and the endorsement model are expected to change
before any mainnet consideration.

## License

Inherits the license of the upstream Canopy repository.

## Contact

[![Twitter](https://img.shields.io/twitter/url/http/shields.io.svg?style=social)](https://x.com/CNPYNetwork)
[![Discord](https://img.shields.io/badge/discord-online-blue.svg)](https://discord.gg/pNcSJj7Wdh)
