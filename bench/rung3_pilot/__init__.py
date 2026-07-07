"""Rung 3 go/no-go pilot — judge-vs-grounded disagreement measurement.

This package is the Phase-0 experiment from ``docs/research/rung3-plan.md``. It
does NOT build the data factory. It answers one question cheaply, before any
factory is built: **do LLM-judge harm labels disagree with execution-grounded
harm labels often enough — and in a direction where the judge is provably
wrong — to justify the whole grounded-labels research contribution?**

Two arms score the same ``(command, workspace)`` pairs:

- **grounded** (:mod:`grounded`) — runs the command in blast-scope's overlay
  CoW sandbox and derives a harm label from the *observed* filesystem diff plus
  the workspace's git state. Needs Linux + unprivileged namespaces.
- **judge** (:mod:`judge`) — asks an LLM (DeepSeek via OpenRouter) to *predict*
  the harm label from the command and a textual view of the workspace, without
  executing anything. Needs an ``OPENROUTER_API_KEY``. Runs anywhere.

The two arms meet at JSON, so they can run on different machines.
:mod:`compare` then computes Cohen's κ, the confusion matrix, and dumps every
disagreement with its diff for hand adjudication.
"""
