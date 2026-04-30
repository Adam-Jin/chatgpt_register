## ADDED Requirements

### Requirement: Shared path resolver
The project SHALL resolve configuration, database, queue, output, and token paths through a shared runtime path resolver.

#### Scenario: Module needs a runtime path
- **WHEN** a module needs `config.json`, `data.db`, `registered_accounts.txt`, `pending_oauth.txt`, or `codex_tokens/`
- **THEN** it obtains the path from the shared resolver instead of deriving it from the module's own `__file__`

### Requirement: Lazy resolution at point of use
Callers of the shared resolver SHALL invoke it at the point of use rather than capturing its return value in a module-level constant or default argument value, except where the surrounding setup itself runs at import.

#### Scenario: New module reads a runtime file
- **WHEN** a function in a package module opens `config.json`, `data.db`, or another runtime file
- **THEN** the function calls the resolver inside the function body so environment overrides set after import are honored

#### Scenario: Default argument values
- **WHEN** a class or function exposes a path parameter
- **THEN** the default is `None` (or another sentinel) and the resolver is called inside the body when the caller did not supply a value, instead of `def fn(..., db_path=DB_PATH)` capturing at definition time

#### Scenario: Import-time setup exception
- **WHEN** module-level code already runs other import-time work (e.g., loading config at import) and that work needs a path
- **THEN** the path may be captured at import alongside the rest of that block, but the constraint that env overrides must be set before importing this module is documented at the capture site

### Requirement: Config path precedence
The project SHALL use deterministic precedence for locating the active configuration file.

#### Scenario: Explicit config override
- **WHEN** a CLI config argument or `CHATGPT_REGISTER_CONFIG` is provided
- **THEN** that path is used as the active config file

#### Scenario: Legacy config compatibility
- **WHEN** no explicit config override is provided and a legacy root `config.json` exists in a source checkout
- **THEN** the legacy root `config.json` remains the active config file

#### Scenario: Default config location
- **WHEN** no explicit config override and no legacy root config exist
- **THEN** the active config path is resolved under the active data directory

### Requirement: Data directory precedence
The project SHALL use a configurable active data directory for runtime state.

#### Scenario: Data directory override
- **WHEN** `CHATGPT_REGISTER_DATA_DIR` or an equivalent CLI option is provided
- **THEN** runtime state paths are resolved under that directory unless a more specific file path override is supplied

#### Scenario: Source checkout default
- **WHEN** the project is run from a source checkout and no data directory override is provided
- **THEN** new runtime state defaults to `<project-root>/var`

#### Scenario: Installed package default
- **WHEN** no project root is discoverable and no data directory override is provided
- **THEN** new runtime state defaults to `./var` relative to the current working directory

### Requirement: Legacy runtime file compatibility
The project SHALL preserve access to existing root-level runtime files during migration.

#### Scenario: Existing legacy database
- **WHEN** a legacy root `data.db` exists and no database path override is provided
- **THEN** the project uses the existing root database rather than creating a new empty database elsewhere

#### Scenario: Existing pending OAuth queue
- **WHEN** a legacy root `pending_oauth.txt` exists and retry-oauth runs without an explicit input file
- **THEN** the retry flow reads the existing queue

#### Scenario: Existing token directory
- **WHEN** a legacy root `codex_tokens/` exists and no token directory override is provided
- **THEN** token output continues to use the existing directory

### Requirement: Runtime artifacts outside package source
The project MUST NOT write mutable runtime artifacts under `src/chatgpt_register/`.

#### Scenario: Main flow writes outputs
- **WHEN** the main flow writes account summaries, OAuth queues, databases, logs, or tokens
- **THEN** those files are written to the active data directory, explicit configured paths, or legacy compatibility paths, never to package source

### Requirement: Relative path resolution
Relative runtime paths from configuration SHALL resolve against the active data directory unless explicitly documented otherwise.

#### Scenario: Relative output file
- **WHEN** `output_file` is set to a relative path
- **THEN** the path resolves under the active data directory

#### Scenario: Absolute output file
- **WHEN** `output_file` is set to an absolute path
- **THEN** the absolute path is used unchanged

### Requirement: Sensitive artifact ignore rules
The repository SHALL document and ignore generated or secret-bearing runtime artifacts.

#### Scenario: Git status after a run
- **WHEN** the tool creates `var/`, `data.db`, result files, pending queues, token files, or local config containing secrets
- **THEN** those artifacts are excluded from normal version control tracking and documented as sensitive
