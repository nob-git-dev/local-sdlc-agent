# Notice

Local SDLC Agent is a source-available research preview.

It is intended to demonstrate a document-mediated, multi-role coding agent architecture
for local LLMs. The project emphasizes specification-driven development, isolated API
calls per role/function, deterministic runner checks, execution evidence, failure
analysis, and controlled repair loops.

## Public License Position

- Non-commercial use: allowed under the public license terms in `LICENSE`
- Commercial use: requires a separate written commercial license
- Production warranty: none
- Security warranty: none

This repository should not be described as an OSI-approved open source project. A more
accurate description is:

```text
Source-available research preview.
Non-commercial use is free under the public license.
Commercial use requires a separate license.
No production warranty.
```

## Safety

The software can read files, call local LLM APIs, apply generated changes, and run test
commands when configured to do so. Review commands and generated changes before using it
on important projects.
