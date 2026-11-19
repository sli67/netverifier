"""The reference-documentation index: vendor manuals and RFCs, searched by meaning and by keyword.

Built with LangChain: documents are split with a text splitter and stored in
Postgres through the PGVector vector store. Embeddings come from OpenAI, so
document text (at build time) and search queries are sent to that provider.

Build or rebuild the index with:  python -m verifier_worker.docs
"""

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

log = logging.getLogger("docs")

COLLECTION = "reference-docs"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150
BATCH = 128
RESULTS = 5
# Hybrid search: each retriever proposes this many chunks. The default merge keeps
# meaning search's top three and adds keyword hits it missed. Measured on queries
# with a known right passage, that beat meaning search alone (13 of 16 against 12),
# while reciprocal rank fusion did worse (11): keyword noise pushed out right answers.
CANDIDATES = 12
FUSION_K = 60
# The keyword side is Postgres full-text search over the same chunk table that
# langchain-postgres creates; this index is ours, made when the index is opened or built.
KEYWORD_INDEX = "langchain_pg_embedding_document_fts_idx"
WORD = re.compile(r"[A-Za-z0-9][\w.:/-]*")
MAX_RESULT_CHARS = 1100
# Notebook outputs are mostly wide tables; the first part shows the columns, which is what matters.
NOTEBOOK_OUTPUT_CHARS = 700

CORPUS = Path(__file__).resolve().parents[2] / "corpus"


def make_embeddings(api_key: str, model: str) -> Embeddings:
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(model=model, api_key=api_key, max_retries=3)


def sqlalchemy_url(database_url: str) -> str:
    """LangChain's vector store connects through SQLAlchemy with the psycopg 3 driver."""
    return re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", database_url)


def open_store(database_url: str, embeddings: Embeddings, reset: bool = False):
    from langchain_postgres import PGVector

    return PGVector(
        embeddings=embeddings,
        collection_name=COLLECTION,
        connection=sqlalchemy_url(database_url),
        use_jsonb=True,
        create_extension=False,  # created by the API's migrations
        pre_delete_collection=reset,
    )


# --- turning each source format into plain text ----------------------------


def html_to_text(html: str) -> str:
    """The main content of a vendor documentation page, without navigation or scripts."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#chapterContent") or soup.select_one("main") or soup.body or soup
    for tag in main.select("script, style, nav, noscript"):
        tag.decompose()
    lines = (line.strip() for line in main.get_text("\n").splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def notebook_to_text(raw: str) -> str:
    """A Jupyter notebook as text: prose, code, and the start of each text output."""
    blocks = []
    for cell in json.loads(raw).get("cells", []):
        source = "".join(cell.get("source", [])).strip()
        if not source:
            continue
        if cell.get("cell_type") != "code":
            blocks.append(source)
            continue
        block = f"```python\n{source}\n```"
        for output in cell.get("outputs", []):
            text = output.get("text") or output.get("data", {}).get("text/plain") or ""
            text = "".join(text).strip()
            if text:
                block += "\nOutput:\n" + text[:NOTEBOOK_OUTPUT_CHARS]
                break
        blocks.append(block)
    return "\n\n".join(blocks)


def selected_pages(total: int, ranges: list[list[int]] | None) -> list[int]:
    """1-based page numbers to index: every page, or only those in the given inclusive ranges."""
    if not ranges:
        return list(range(1, total + 1))
    pages: set[int] = set()
    for first, last in ranges:
        pages.update(range(max(1, first), min(total, last) + 1))
    return sorted(pages)


def load_source(source: dict[str, Any], raw: Path) -> list[Document]:
    """Read one corpus file into documents: one per page for PDFs, one per file otherwise."""
    path = raw / source["path"]
    meta = {k: source.get(k) for k in ("vendor", "os", "version", "title")}
    meta["source"] = source["path"]

    if path.suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        docs = []
        for number in selected_pages(len(reader.pages), source.get("pages")):
            text = (reader.pages[number - 1].extract_text() or "").strip()
            if text:
                docs.append(Document(page_content=text, metadata={**meta, "page": number}))
        return docs

    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".html":
        text = html_to_text(text)
    elif path.suffix == ".ipynb":
        text = notebook_to_text(text)
    return [Document(page_content=text, metadata=meta)] if text.strip() else []


def split(documents: Iterable[Document]) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_CHARS, chunk_overlap=CHUNK_OVERLAP)
    return splitter.split_documents(list(documents))


# --- searching ---------------------------------------------------------------


def keyword_query(text: str) -> str | None:
    """A websearch_to_tsquery string matching chunks that contain any of the query's words.

    Words are joined with "or" so a long natural-language query still matches;
    ts_rank_cd then favours chunks with more of the words, closer together.
    Hyphens, colons and dots stay inside words, so command names such as
    as-path-expand and values such as 65535:666 survive.
    """
    words = list(dict.fromkeys(w.lower() for w in WORD.findall(text) if w.lower() != "or"))
    return " or ".join(words) if words else None


def fuse(rankings: list[list[Document]], limit: int = RESULTS, k: int = FUSION_K) -> list[Document]:
    """Reciprocal rank fusion: a chunk scores 1 / (k + rank) in each ranking it appears in."""
    scores: dict[str, float] = {}
    chunks: dict[str, Document] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            key = doc.id or str(hash(doc.page_content))
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            chunks.setdefault(key, doc)
    return [chunks[key] for key in sorted(scores, key=lambda key: scores[key], reverse=True)[:limit]]


def interleave(meaning: list[Document], keyword: list[Document], keep: int = 3, limit: int = RESULTS) -> list[Document]:
    """Meaning search's best `keep`, then keyword hits it did not find, then the rest of meaning search's list."""
    out, seen = [], set()
    for doc in meaning[:keep] + keyword + meaning[keep:]:
        key = doc.id or str(hash(doc.page_content))
        if key not in seen:
            seen.add(key)
            out.append(doc)
    return out[:limit]


def ensure_keyword_index(database_url: str) -> None:
    """Create the full-text index over the chunk table, if the table exists and the index does not."""
    import psycopg

    with psycopg.connect(database_url, autocommit=True) as conn:
        if conn.execute("select to_regclass('public.langchain_pg_embedding')").fetchone()[0] is None:
            return
        conn.execute(
            f"create index if not exists {KEYWORD_INDEX} on langchain_pg_embedding "
            "using gin (to_tsvector('english', document))"
        )


class DocsIndex:
    """Searches the documentation index and formats results for the agent."""

    def __init__(self, store, database_url: str | None = None):
        self._store = store
        self._database_url = database_url

    def keyword_search(self, query: str, vendor: str | None, limit: int = CANDIDATES) -> list[Document]:
        import psycopg

        terms = keyword_query(query)
        if not terms or not self._database_url:
            return []
        # The query is built from the same lexemes the index holds. A hyphenated name in
        # websearch_to_tsquery becomes a strict phrase that does not line up with how the
        # document was indexed, and so matches nothing. Chunks with every word come first
        # (exact names and values such as 65535:666), then chunks with any of them.
        sql = """
            with query as (
              select to_tsquery('simple', string_agg(quote_literal(word), %(join)s)) as q
                from unnest(tsvector_to_array(to_tsvector('english', %(terms)s))) as word
            )
            select e.id, e.document, e.cmetadata
              from langchain_pg_embedding e
              join langchain_pg_collection c on c.uuid = e.collection_id,
                   query
             where q is not null
               and c.name = %(collection)s
               and to_tsvector('english', e.document) @@ query.q
               and (%(vendor)s::text is null or e.cmetadata->>'vendor' = %(vendor)s)
             order by ts_rank_cd(to_tsvector('english', e.document), query.q) desc
             limit %(limit)s
        """
        rows, seen = [], set()
        with psycopg.connect(self._database_url) as conn:
            for join in (" & ", " | "):
                params = {"terms": terms, "join": join, "collection": COLLECTION, "vendor": vendor, "limit": limit}
                for row in conn.execute(sql, params).fetchall():
                    if row[0] not in seen and len(rows) < limit:
                        seen.add(row[0])
                        rows.append(row)
        return [Document(id=str(r[0]), page_content=r[1], metadata=r[2] or {}) for r in rows]

    def ranked(self, query: str, vendor: str | None = None, mode: str = "hybrid", merge: str = "interleave") -> list[Document]:
        """The best chunks for a query. mode is hybrid, vector or keyword, and merge interleave or rrf;
        the alternatives are kept for comparing retrieval quality."""
        vendor = (vendor or "").strip().lower() or None
        rankings = []
        if mode in ("hybrid", "vector"):
            rankings.append(self._store.similarity_search(query, k=CANDIDATES, filter={"vendor": vendor} if vendor else None))
        if mode in ("hybrid", "keyword"):
            try:
                rankings.append(self.keyword_search(query, vendor))
            except Exception:  # noqa: BLE001 - keyword search is an addition; meaning search still answers
                log.exception("keyword search failed; using meaning search only")
        if merge == "interleave" and len(rankings) == 2:
            return interleave(*rankings)
        return fuse(rankings)

    def search(self, query: str, vendor: str | None = None) -> str:
        vendor = (vendor or "").strip().lower() or None
        found = self.ranked(query, vendor)
        if not found:
            scope = f" for vendor {vendor!r}" if vendor else ""
            return f"No documentation found{scope}. The index may not cover it, or may not have been built."
        blocks = []
        for i, doc in enumerate(found, start=1):
            m = doc.metadata
            where = m["source"] + (f", page {m['page']}" if m.get("page") else "")
            version = f" {m['version']}" if m.get("version") else ""
            text = doc.page_content.strip()
            if len(text) > MAX_RESULT_CHARS:
                text = text[:MAX_RESULT_CHARS] + " [...]"
            blocks.append(f"[{i}] {m['title']} ({m['vendor']}{version}; {where})\n{text}")
        return "\n\n".join(blocks)


_index: DocsIndex | None = None


def get_index(database_url: str, api_key: str | None, model: str) -> DocsIndex | None:
    """The process-wide index, or None when no embedding key is configured."""
    global _index
    if api_key is None:
        return None
    if _index is None:
        try:
            ensure_keyword_index(database_url)
        except Exception:  # noqa: BLE001 - without the index keyword search is slower, not wrong
            log.exception("could not create the keyword index")
        _index = DocsIndex(open_store(database_url, make_embeddings(api_key, model)), database_url)
    return _index


# --- building ----------------------------------------------------------------


def build(database_url: str, api_key: str, model: str, corpus: Path = CORPUS) -> int:
    """Rebuild the whole index from the files listed in the corpus manifest. Returns the chunk count."""
    manifest = json.loads((corpus / "manifest.json").read_text())["files"]
    present = {entry["path"] for entry in manifest}
    sources = [s for s in json.loads((corpus / "sources.json").read_text())["sources"] if s["path"] in present]

    chunks: list[Document] = []
    for source in sources:
        started = time.monotonic()
        pieces = split(load_source(source, corpus / "raw"))
        chunks.extend(pieces)
        log.info("%-40s %5d chunks (%.1fs)", source["path"], len(pieces), time.monotonic() - started)

    log.info("embedding %d chunks with %s", len(chunks), model)
    store = open_store(database_url, make_embeddings(api_key, model), reset=True)
    started = time.monotonic()
    for offset in range(0, len(chunks), BATCH):
        store.add_documents(chunks[offset : offset + BATCH])
        done = min(offset + BATCH, len(chunks))
        if done == len(chunks) or (offset // BATCH) % 5 == 4:
            log.info("  %d / %d (%.0fs)", done, len(chunks), time.monotonic() - started)
    ensure_keyword_index(database_url)
    return len(chunks)


def main() -> int:
    import os

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for noisy in ("httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.error("OPENAI_API_KEY is not set; add it to worker/.env")
        return 1
    model = os.environ.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
    count = build(os.environ["DATABASE_URL"], api_key, model)
    log.info("index rebuilt: %d chunks", count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
