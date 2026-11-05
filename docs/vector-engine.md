# Vector engine

Checked 5 November 2026 against current docs and upstream repos. This answers section 2 of [research.md](research.md).

Chosen engine: **Qdrant server 1.20.0**, image `qdrant/qdrant:v1.20.0`, Apache 2.0. `scripts/setup` creates `data/qdrant/` and runs that image with the directory mounted at `/qdrant/storage`. The same image and the same storage layout are the later container, including a multi-node deploy. No account. Telemetry is off (`QDRANT__TELEMETRY_DISABLED=true`). The port is bound to loopback because the local process has no API key.

The Python client’s `QdrantClient(path=...)` mode and Qdrant Edge are different storage formats. The setup script starts the server.

## Why this one

The brief asks for one local data directory now and the same engine as a server process later, not a second product. Approved documents stay the source of truth, so the index can be deleted and rebuilt.

Qdrant server is that shape:

- Local setup is `docker run` with a bind mount. No Qdrant account, no API key required for the local process.
- The directory under `data/qdrant/` is the server’s storage. A later container, Compose file, or Helm release mounts the same layout.
- Horizontal scale is the same Apache-2.0 binary. Set `QDRANT__CLUSTER__ENABLED=true` and let peers talk on port 6335. Sharding and replication ship in the open-source server. Qdrant Cloud and the Private Cloud operator add operations on top (automatic rebalancing, resharding, backups, zero-downtime upgrades). They are optional. The engine and the API stay the same.
- Payload indexes cover the locked tags `repo`, `tool`, and `session_id`.
- Re-embed is delete-and-upsert. Wipe the collection, or the storage directory, and embed the approved documents again. Switching engines later discards the index only.

A company of coding-agent memories is a small corpus: approved slices, not raw transcripts. One Qdrant node is the company deployment until the index outgrows one disk. The cluster flag is there when that happens.

Self-hosted defaults have no authentication. A shared deploy sets an API key and does not publish port 6333 unauthenticated. Storage needs a POSIX block device. Qdrant does not run on NFS or S3.

Sources: [Qdrant installation](https://qdrant.tech/documentation/installation/), [distributed deployment](https://qdrant.tech/documentation/scaling/distributed_deployment/), [v1.20.0 release](https://github.com/qdrant/qdrant/releases/tag/v1.20.0) (22 October 2026), [qdrant-client local mode](https://github.com/qdrant/qdrant-client).

## What the setup script does

`scripts/setup` reads [compose.yaml](../compose.yaml) and:

1. Creates `data/qdrant/` (already gitignored via `data/`).
2. Starts `qdrant/qdrant:v1.20.0` with that directory mounted at `/qdrant/storage`. HTTP is `127.0.0.1:6333`, gRPC is `127.0.0.1:6334`.
3. Waits until `GET /readyz` returns.

The embed step talks to `http://127.0.0.1:6333`. It does not open the directory from the client. `scripts/embed` creates the `memories` collection at 384 dimensions, the size of the chosen model.

Pin the image tag. `:latest` moves.

## Candidates

| Engine | Local, no account | Container | Same format locally and as a server | Rebuild from approved documents | License | Fit |
| --- | --- | --- | --- | --- | --- | --- |
| Qdrant server 1.20.0 | `docker run` writes `data/qdrant/` | Official image. Helm chart for Kubernetes | Yes, if the process is the server. Client `path=` mode is a separate SQLite layout and is the wrong local mode | Delete the collection or the storage directory and re-embed | Apache 2.0. Local server and cloud engine are the same project | Recommended |
| pgvector 0.8.7 | Postgres container; data directory is `PGDATA` | `pgvector/pgvector:0.8.7-pg18-trixie` | Yes. Managed Postgres (RDS, Aurora, Cloud SQL, AlloyDB) is the same extension and SQL | Drop the table and re-embed | PostgreSQL license | Runner-up when the company already runs Postgres |
| Chroma | `PersistentClient(path=...)` or `chroma run --path` | `chromadb/chroma`, mount `/data` | Single-node embedded and single-node server share a SQLite plus HNSW layout. Distributed Chroma uses a different storage subsystem | Delete the persist directory and re-embed | Apache 2.0. Product telemetry stopped in 1.5.4 | Fine for one machine. Company scale is a different store |
| LanceDB OSS | `lancedb.connect("data/lancedb")` writes Lance files | No official server image. A container would just run the library | The files are the database, including on object storage. The shared server is LanceDB Enterprise, a separate commercial product | Delete the directory and re-embed | Apache 2.0 for OSS. Enterprise is commercial | Strong local directory. The server is a second product |
| sqlite-vec | One SQLite file plus the extension | A container can hold the file. There is no vector server | Embedded only | Delete the file and re-embed | Apache 2.0 (repo also described as MIT/Apache dual-licensed). Pre-1.0 | Exact KNN, no ANN index, one writer. Stops being the right tool once more than one machine queries it |

### pgvector

`pgvector` 0.8.7 (1 October 2026) on Postgres 13–18. The Docker tags `0.8.7-pg18-trixie` pin the extension; floating tags such as `pg18` move. Postgres 18 images store data at `/var/lib/postgresql/18/docker` and declare the volume at `/var/lib/postgresql`. Mounting the old `/var/lib/postgresql/data` path on a Postgres 18 image leaves the data in an anonymous volume.

Local and server are the same Postgres. Filters on `repo`, `tool`, and `session_id` are ordinary columns. Vertical scale is a larger instance. Horizontal scale is replicas plus something else (Citus, PgDog, or partitioning), which is a second system. For this repo the index is derived, so a full database server is extra operational weight next to documents that already live as files.

Use pgvector if a later deploy must sit inside Postgres the company already operates. Otherwise Qdrant is the smaller same-engine path.

Sources: [pgvector repository](https://github.com/pgvector/pgvector/), [install notes checked 27 October 2026](https://pgvector.app/install/).

### Chroma

Apache 2.0. Single-node Chroma persists `chroma.sqlite3` and per-collection HNSW segments under a directory. Docker mounts that directory at `/data`. `PersistentClient` is in-process and not safe for several writer processes.

Chroma’s own open-source page says distributed Chroma and single-node local Chroma rely on different storage subsystems, and that current engineering effort is on the distributed system and the cloud. Moving a company deploy onto distributed Chroma is a storage change, then a re-embed. That breaks the “one format now and later” preference.

Sources: [Chroma open source](https://docs.trychroma.com/docs/overview/oss), [Docker](https://docs.trychroma.com/guides/deploy/docker).

### LanceDB

Apache 2.0 embedded library. `connect` on a directory creates Lance files. The same library can open `s3://`, `gs://`, or `az://`. Reads scale with the storage. Concurrent writers retry commits and can fail under contention.

There is no open-source server process. LanceDB Enterprise is the commercial remote (`db://`) and the documented path past tens of millions of vectors toward billions. That is a second product, which this brief rules out.

Source: [LanceDB OSS FAQ](https://docs.lancedb.com/faq/faq-oss).

### sqlite-vec

SQLite extension, pre-1.0, Apache 2.0. `vec0` stores float, int8, and binary vectors and scans them. It does not build an HNSW or IVF index. One file, many readers, one writer. SQLite over a network filesystem is the wrong deployment. A container does not turn it into a shared company service.

Source: [asg017/sqlite-vec](https://github.com/asg017/sqlite-vec).

## Gap

No candidate is both a library call that creates `data/` and a multi-node server with one on-disk format, unless the local step is already the server.

- Qdrant’s in-process local mode looks like that library call and writes SQLite files the server cannot open. Edge is a beta in-process shard with its own sync protocol. Both are out.
- Chroma’s library call matches its single-node server and diverges at distributed scale.
- LanceDB’s library call is the real database, and the company server is Enterprise.
- sqlite-vec’s library call has no server behind it.
- pgvector is the same engine at every size and needs a Postgres process even for the first index.

The gap is closed by starting Qdrant as the server immediately, with `data/qdrant/` as its storage mount.
