# File transfer repository

- This is a public repository for sharing standalone scripts, documentation, and small files across environments.
- Put runnable helpers in `scripts/`, usage notes in `docs/`, and CPU-only regression tests in `tests/`.
- Keep shared scripts independent of local workspace paths and private infrastructure.
- Publish only the intended shareable files; never copy the entire surrounding workspace or include credentials and private runtime output.
- Run relevant CPU checks locally. Hardware validation runs on the user's target machine.
- Preserve Linux-compatible line endings. Push updates to this repository's `origin`.
