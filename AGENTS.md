# Agent Instructions

## Project Overview
You are an expert developer specializing in NVDA (NonVisual Desktop Access) add-on development.
This project is an NVDA add-on.

## Context & References
When developing or refactoring, you MUST adhere to the standards defined in the official NVDA core repository.
- **Reference Source Code**: `d:\git\nvda\source` (Refer to this for core logic, synth settings, and existing add-on implementations).
- **Technical Design**: Refer to `D:\git\nvda\projectDocs\design\technicalDesignOverview.md`.
- **Coding Standards**: STRICTLY FOLLOW `D:\git\nvda\projectDocs\dev\codingStandards.md`.
- **Developer Guide**: Consult `D:\git\nvda\projectDocs\dev\developerGuide\developerGuide.md` for API usage.

## Coding Conventions (Priority)
1. **Indentation**: Use **Tabs**, not spaces (as per NVDA's `codingStandards.md`).
2. **Naming**: Prioritize NVDA's specific camelCase or underscore preferences as found in the core source.
3. **Docstrings**: All classes and functions must have descriptive docstrings in English.
4. **I18n**: Use `_()` for all user-facing strings to ensure translatability.

## Development Workflow
- **Linting**: Before suggesting code, ensure it passes ruff checks based on config.
- **Create package**: `scons` can create plugin packages. Before creating a plugin package, you can run `scons -c` to clean it up.
- **Testing**: The current environment cannot run unit tests. You can use Python static compilation to ensure there are no syntax errors. Once everything is complete, you can use `explorer`<addonPackage> Perform the installation and allow users to manually test the features.
