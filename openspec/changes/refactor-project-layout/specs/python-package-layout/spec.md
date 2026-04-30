## ADDED Requirements

### Requirement: Canonical source package
The project SHALL place importable application code under `src/chatgpt_register/`.

#### Scenario: Package imports from editable install
- **WHEN** the project is installed in editable mode
- **THEN** `import chatgpt_register` imports the package from `src/chatgpt_register/`

#### Scenario: Source files are not imported from repository root
- **WHEN** tests import application modules
- **THEN** they import modules through `chatgpt_register.*` rather than root-level module names

### Requirement: Main compatibility script wrapper
The project SHALL preserve the root-level main script command as a thin compatibility wrapper.

#### Scenario: Main wrapper starts the packaged main flow
- **WHEN** a user runs `python3 chatgpt_register.py`
- **THEN** the wrapper delegates to the package-owned main CLI without duplicating business logic

#### Scenario: Helper console scripts
- **WHEN** a user runs `sentinel-solver --thread 2` or `phone-pool <command>` after installing the project
- **THEN** the console script delegates to the corresponding package-owned CLI implementation

### Requirement: Module entrypoint
The package SHALL provide a module entrypoint for the main command.

#### Scenario: Module execution
- **WHEN** a user runs `python -m chatgpt_register` in an environment where the package is importable
- **THEN** the same main CLI behavior is invoked as `python3 chatgpt_register.py`

### Requirement: Packaging metadata
The project SHALL define package metadata and entrypoints in `pyproject.toml`.

#### Scenario: Editable installation
- **WHEN** a developer runs an editable install for the project
- **THEN** Python can import `chatgpt_register` and console entrypoints resolve to package-owned functions

#### Scenario: Core dependency declaration
- **WHEN** dependencies are reviewed
- **THEN** every import that the default CLI flow performs at startup resolves through `[project].dependencies` in `pyproject.toml`

#### Scenario: Single project dependency source
- **WHEN** dependencies for built-in project commands are reviewed
- **THEN** they are declared in `[project].dependencies` in `pyproject.toml`, not in separate requirement files or command-specific requirement sets

#### Scenario: Optional landbridge dependencies
- **WHEN** private or less-common landbridge dependencies are reviewed
- **THEN** they may remain under `[project.optional-dependencies].landbridge` so a plain editable install does not require private repository access

### Requirement: Explicit internal imports
Internal package modules MUST use package-relative or package-qualified imports for other project modules.

#### Scenario: Import after package relocation
- **WHEN** a module under `src/chatgpt_register/` imports `phone_pool`, `sms_provider`, `monitor`, or other project modules
- **THEN** it uses an explicit package import that works outside the repository root

### Requirement: Root script must not shadow the package
The root `chatgpt_register.py` wrapper SHALL keep `import chatgpt_register` resolving to the `src/chatgpt_register/` package even when the script has already been loaded as `__main__`.

#### Scenario: Submodule import after running the wrapper
- **WHEN** the root wrapper is executed (`python3 chatgpt_register.py`) and code subsequently imports `chatgpt_register.cli` or any other package submodule
- **THEN** the submodule is loaded from `src/chatgpt_register/`, not from the root script directory

#### Scenario: Legacy top-level attribute access
- **WHEN** legacy callers do `from chatgpt_register import ChatGPTRegister` (or any name in the documented legacy export allowlist) against the root wrapper
- **THEN** the wrapper resolves the attribute by importing the corresponding name from `chatgpt_register.register`, without duplicating business logic in the wrapper

#### Scenario: Unknown attribute on the wrapper
- **WHEN** code accesses an attribute on the root wrapper that is not in the legacy export allowlist
- **THEN** an `AttributeError` is raised, so attribute-presence checks behave correctly
