# Embedding model

Checked 5 November 2026. This answers section 3 of [research.md](research.md).

Default model: **BAAI/bge-small-en-v1.5**, embedded from the quantized ONNX checkpoint `Qdrant/bge-small-en-v1.5-onnx-Q` at revision `aa8f8b060edb00e03bfdd08813a2949946c8ba55`. The upstream weights are `BAAI/bge-small-en-v1.5` at `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`. FastEmbed 0.9.1 runs that ONNX file. `scripts/embed` downloads the pinned revision into `data/models/bge-small-en-v1.5-onnx-q/` and upserts the approved rows of `data/memories.sqlite`.

| | |
| --- | --- |
| Dimension | 384 |
| Distance | Cosine |
| License | MIT. Commercial use of the model weights is allowed. FastEmbed itself is Apache 2.0 |
| GPU | None. ONNX Runtime on CPU |
| Size | About 67 MB quantized. 33.4 million parameters. 512 token input |
| CPU cost | On this machine, loading the ONNX file took 0.13s and embedding the one-sentence decision took 0.01s |

Documents are embedded with no query instruction. BGE v1.5 still suggests prepending `Represent this sentence for searching relevant passages: ` to a short query at retrieval time. Passages do not get that prefix. Retrieval is not built yet. A later query path has to use the same checkpoint and that split, or the scores will not match these vectors.

A second machine gets the same vectors by running `scripts/embed`, which fetches revision `aa8f8b060edb00e03bfdd08813a2949946c8ba55` and FastEmbed 0.9.1. The collection is `memories`. Switching checkpoints means deleting that collection and embedding the documents again.

A hosted model is a different vector space, so it gets its own collection and a full reindex. It is never mixed into an index built with this one. A later cloud deploy runs this same ONNX file next to Qdrant. Approved documents stay the source of truth either way.


## Choosing the embedder

[`scripts/embedder.py`](../scripts/embedder.py) holds the interface. `scripts/embed` asks it for the configured embedder, and a query path asks for the same one. Two environment variables choose it:

| Variable | Default | Today |
| --- | --- | --- |
| `AGENT_MEMORY_EMBEDDER` | `fastembed` | Only `fastembed` is implemented. Any other value stops `scripts/embed` with an error that names this section. |
| `AGENT_MEMORY_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed runs only this model, at the pinned revision above. |

An embedder names its `provider`, `model`, `revision`, `dimension`, and `distance`. Each point's payload records `embedding_model` and `embedding_revision`. The vector length is checked against `dimension` before anything is written, and an existing collection of another size is refused.

## Adding a provider

OpenAI and AWS Bedrock are not implemented. They are left for a later plan PR, as `AGENTS.md` asks. This is how one would plug in:

1. Write a class in `scripts/embedder.py` with the five attributes and three methods of `Embedder`:
   - `load()` may do nothing for a hosted model.
   - `embed_documents(texts)` embeds passages as they are.
   - `embed_query(text)` adds whatever instruction that model expects for a query.
2. Fix `dimension` for the deployment. Both candidates can return shorter vectors on request: OpenAI's `dimensions` parameter for `text-embedding-3-small`, and Bedrock Titan Text Embeddings v2's 1024, 512, or 256. Pick one, and never vary it per call.
3. Use `revision` for the provider's version string, such as the Bedrock model id `amazon.titan-embed-text-v2:0`. A model name that has no version needs a date or a release name, so a later silent upgrade can be detected.
4. Register the class in `PROVIDERS` under its provider name.
5. Read credentials only from the process environment or the provider's default chain: `OPENAI_API_KEY`, or the AWS credential chain with an IAM role. Never write them to SQLite, payloads, or logs.
6. Handle the provider's batch limits, retries, and rate limits inside the adapter. Unit tests use a fake transport and no network.
7. Check the model names, sizes, and limits above against the provider's current docs before you build. They are from memory, not checked for this note.

A hosted provider sends the approved text to that provider. Review is still the redaction step, and nothing else leaves the machine: no transcript, and no draft that was never approved. Whether a deployment may send approved text to that provider is the deployment's decision to make.

Switching an existing index to another model means embedding every approved row again into a collection made for that model. Today that is `scripts/embed --rebuild` with the new `AGENT_MEMORY_EMBEDDER`. [server-plan.md](server-plan.md) moves this to the server's `reindex`, which keeps one collection per model and refuses to start on a mismatch.

Sources: [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5), [Qdrant/bge-small-en-v1.5-onnx-Q](https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q), [FastEmbed 0.9.1 model list](https://qdrant.tech/documentation/embeddings/fastembed/).
