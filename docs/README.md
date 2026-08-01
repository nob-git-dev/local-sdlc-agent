# Documentation

This directory contains public-facing documentation for Local SDLC Agent.

## Visual Guide

The comprehensive Japanese implementation guide is published as a single-page
website:

- **Published site:** [Local SDLC Agent — Visual Code Guide](https://nob-git-dev.github.io/local-sdlc-agent/)
- **HTML source:** [`architecture/local_sdlc_agent_visual_guide_20260724.html`](architecture/local_sdlc_agent_visual_guide_20260724.html)

It explains the architecture, supervisor routing, coding-agent repair loop,
staged execution, prompt contracts, artifact controls, verification evidence,
major routines, and current implementation limitations with interactive
diagrams and charts.

## Active Specifications

- **Autonomous execution plane:** [`architecture/autonomous_supervisor_runtime_spec.md`](architecture/autonomous_supervisor_runtime_spec.md)
- **Experience learning control plane:** [`../learning-runtime/SPEC.md`](../learning-runtime/SPEC.md)

## Structure

| Directory | Purpose |
|---|---|
| `usage/` | How the agent flow works from a user's point of view. |
| `architecture/` | Design notes, control models, role/function separation, and implementation architecture. |
| `research/` | Benchmark notes and model/profile comparison records that are useful for reproducibility. |
| `history/` | Dated implementation notes and lessons that explain why specific controls were added. |

## Excluded Documents

Patent-oriented notes and note.com / Zenn article drafts are intentionally not
kept in this public documentation tree.

Those documents are local working materials, not required for using or
understanding the public package.
