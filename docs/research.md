# Research questions

Answer these before adding a dependency, a vector engine, or an embedding model. Record what you found, including the gap. Do not adopt a project because it is on this list.

Sources to check are current docs and the upstream repos. Versions move. The notes in [harnesses.md](harnesses.md) are a starting map, not a finished API reference.

## 1. What already does this

The job is specific: capture a session from more than one coding harness, then let a person keep or drop slices before anything is embedded.

Look at:

- mem0
- Letta
- Zep
- Graphiti
- cognee
- claude-mem
- basic-memory

For each one, write:

- What it stores, and whether a person chooses before the write.
- Which harnesses it hooks, if any.
- Whether the index can be rebuilt from approved documents.
- License.
- The gap relative to this repo. A gap of “no multi-harness capture” or “no keep/drop step” is enough to leave it as a reference.

## 2. Vector engine

Need a script that creates the database in this repo’s `data/` directory, and a path to run that same engine as a container later. One local data directory now. A server process later. Not a second product.

Candidates:

- Qdrant
- Chroma
- pgvector
- LanceDB
- sqlite-vec

For each one, write:

- How a setup script creates it locally, with no account.
- How the same engine runs as a container.
- Whether approved documents can be re-embedded into a fresh database.
- What you would have to throw away if you switched engines later. The documents are the source of truth, so the answer should be “the index only”.
- License, and whether the local mode is the same project as the server mode.

Prefer the engine whose local directory and server process are the same format. Recommend one. Do not add it until that recommendation is written down.

## 3. Embedding model

An open-source model the same setup script can pull and run.

For the model you recommend, record:

- Name and exact revision or checkpoint.
- Dimension.
- License.
- Whether it needs a GPU, and what it costs to run on CPU.
- How the setup script fetches it so a second machine gets the same vectors.

If a hosted API is clearly better for a later cloud deploy, say so as a second run mode. The local script still has to work without that API.

## 4. Hook facts still unchecked

Confirm against current docs, then correct [harnesses.md](harnesses.md) if the note is wrong:

- Pi’s actual hook or extension API: event name, payload, and config path for “session finished, transcript readable”.
- Cursor `sessionEnd` stdin fields. `stop` is the capture event. `sessionEnd` is only a marker until those fields are verified.
- Claude Code `Stop` versus `SessionEnd`: confirm `transcript_path` is present on `Stop` and unreliable on `SessionEnd`, including the worktree path bug.
- Codex `Stop` and `SessionEnd`: confirm the timeout cap and that `SessionEnd` is the wrong place to read a transcript.

## Done when

The research note names:

- the projects that do not cover the keep/drop step, and why
- one vector engine
- one embedding model
- corrected hook facts for Pi and for Cursor `sessionEnd`

Then implementation can start. The first build is capture, review, and embed. Retrieval at session start stays out.
