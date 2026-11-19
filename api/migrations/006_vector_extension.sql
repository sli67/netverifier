-- Vector similarity search, used by the documentation index. The index's own
-- tables (langchain_pg_collection, langchain_pg_embedding) are created and
-- managed by the worker's LangChain vector store, not by these migrations.
create extension if not exists vector;
