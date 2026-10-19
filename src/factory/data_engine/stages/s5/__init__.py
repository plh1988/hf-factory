"""Stage s5 internals: claims, authorizations and the claim-domain writers.

  inputs.py   what s5 reads from earlier stages (Inputs) and provider picking
  claims.py   one shard's claims and lines (ClaimBatch), built step by step
  auths.py    authorizations for a shard (AuthBatch) and their entities
  writers.py  appeals, reprocessing, and every claim entity written from a ClaimBatch

The stage entry points (prepare, task_count, run_task) are in stages/s5_claims_auth.py.
"""
