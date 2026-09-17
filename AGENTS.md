# Agent Instructions

## Communication

- Use direct language and layman's terminology.
- Use rhythmic and balanced naming conventions.

## Documentation

- Keep documentation updated and lean.
- Prefer presenting information in a listicle style.
- Prefer flat lists over tables.

### File Descriptions

- AGENTS.md: agent instructions
- CHANGES.md: exclusively for current status, version list, decision rationale
- DEMO.ipynb: exclusively for minimal usage example
- GUIDE.md: exclusively for complete project implementation overview and reference
- README.md: exclusively for summary, highlights, quickstart
- TODO.md: exclusively for flat list of open items sorted by impact and tagged by category

## Environment

- Always use setup.sh/setup.ps1 to install missing resources

### File Descriptions

- setup.ps1: install required environment tools (Windows)
- setup.sh: install required environment tools (Mac/Linux)

## Source Code

### File Descriptions

- agent.py: the whole implementation — `Agent` and `Engine`
- agent_test.py: tests
- agent_ui.html / agent_ui.css / agent_ui.js: web UI served by `Agent`
- pyproject.toml: package metadata and dependencies
