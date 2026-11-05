# Embedding model

Checked 5 November 2026. This answers section 3 of [research.md](research.md).

Chosen model: **BAAI/bge-small-en-v1.5**, embedded from the quantized ONNX checkpoint `Qdrant/bge-small-en-v1.5-onnx-Q` at revision `aa8f8b060edb00e03bfdd08813a2949946c8ba55`. The upstream weights are `BAAI/bge-small-en-v1.5` at `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`. FastEmbed 0.9.1 runs that ONNX file. `scripts/embed` downloads the pinned revision into `data/models/bge-small-en-v1.5-onnx-q/` and upserts `memories/*.json`.

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

A hosted embeddings API is a different vector space. It is not a second run mode for this index. A later cloud deploy runs this same ONNX file next to Qdrant. Approved documents stay the source of truth either way.

Sources: [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5), [Qdrant/bge-small-en-v1.5-onnx-Q](https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q), [FastEmbed 0.9.1 model list](https://qdrant.tech/documentation/embeddings/fastembed/).
