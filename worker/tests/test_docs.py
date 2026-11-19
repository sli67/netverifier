"""Document conversion, page selection and result formatting. No model or database needed."""

from langchain_core.documents import Document

from verifier_worker import docs

PAGE = """<html><head><script>var x = 1;</script><style>p { color: red }</style></head>
<body><nav>Home | Products | Support</nav>
<div id="chapterContent"><h1>BGP Route-Map Continue</h1>
<p>The continue clause   allows further entries to be evaluated.</p>


<pre>route-map OUT permit 10
 continue 20</pre><script>track()</script></div>
<footer>Copyright</footer></body></html>"""


def test_html_keeps_the_chapter_text_and_drops_page_chrome():
    text = docs.html_to_text(PAGE)
    assert text.startswith("BGP Route-Map Continue")
    assert "route-map OUT permit 10\ncontinue 20" in text
    for unwanted in ("Home | Products", "Copyright", "track()", "var x", "color: red"):
        assert unwanted not in text
    assert "\n\n\n" not in text


def test_selected_pages_uses_inclusive_ranges_and_clamps_to_the_document():
    assert docs.selected_pages(4, None) == [1, 2, 3, 4]
    assert docs.selected_pages(10, [[2, 4], [9, 20]]) == [2, 3, 4, 9, 10]
    assert docs.selected_pages(10, [[3, 5], [4, 6]]) == [3, 4, 5, 6]


def test_load_source_tags_each_document_and_split_keeps_the_tags(tmp_path):
    (tmp_path / "frr").mkdir()
    (tmp_path / "frr" / "routemap.rst").write_text("Route Maps\n==========\n\n" + "A route-map entry. " * 200)
    source = {"path": "frr/routemap.rst", "vendor": "frr", "os": "frr", "version": "10.6.2", "title": "FRR: Route Maps"}

    loaded = docs.load_source(source, tmp_path)
    assert [d.metadata for d in loaded] == [
        {"vendor": "frr", "os": "frr", "version": "10.6.2", "title": "FRR: Route Maps", "source": "frr/routemap.rst"}
    ]
    chunks = docs.split(loaded)
    assert len(chunks) > 2
    assert all(len(c.page_content) <= docs.CHUNK_CHARS and c.metadata["vendor"] == "frr" for c in chunks)


class FakeStore:
    def __init__(self, results):
        self.results, self.calls = results, []

    def similarity_search(self, query, k, filter=None):
        self.calls.append((query, k, filter))
        return self.results


def test_search_formats_results_with_their_source_and_filters_by_vendor():
    store = FakeStore(
        [
            Document(
                page_content="A policy-statement is evaluated term by term.",
                metadata={"vendor": "juniper", "version": "current", "title": "Junos Routing Policies", "source": "juniper/routing-policy.pdf", "page": 120},
            ),
            Document(page_content="x" * 5000, metadata={"vendor": "ietf", "version": None, "title": "RFC 4271", "source": "rfc/rfc4271.txt"}),
        ]
    )
    out = docs.DocsIndex(store).search("term evaluation", " Juniper ")
    assert store.calls == [("term evaluation", docs.CANDIDATES, {"vendor": "juniper"})]
    first, second = out.split("\n\n")
    assert first == (
        "[1] Junos Routing Policies (juniper current; juniper/routing-policy.pdf, page 120)\n"
        "A policy-statement is evaluated term by term."
    )
    assert second.startswith("[2] RFC 4271 (ietf; rfc/rfc4271.txt)\n") and second.endswith("[...]")

    docs.DocsIndex(store).search("anything")
    assert store.calls[-1][2] is None


def test_search_says_so_when_nothing_is_found():
    assert "No documentation found for vendor 'arista'" in docs.DocsIndex(FakeStore([])).search("mlag", "arista")


def test_sqlalchemy_url_selects_the_psycopg_driver():
    assert docs.sqlalchemy_url("postgres://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"
    assert docs.sqlalchemy_url("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d"


def test_notebook_keeps_prose_code_and_the_start_of_text_output():
    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": ["## Test Route Policies\n", "Evaluates a route against a policy."]},
            {
                "cell_type": "code",
                "source": ["result = bf.q.testRoutePolicies(policies=\"OUT\", direction=\"out\").answer().frame()"],
                "outputs": [
                    {"output_type": "execute_result", "data": {"text/html": ["<table>ignored</table>"], "text/plain": ["Node  Action\n", "edge1 PERMIT"]}}
                ],
            },
            {"cell_type": "code", "source": [], "outputs": []},
            {"cell_type": "code", "source": ["print(1)"], "outputs": [{"output_type": "stream", "text": ["Z" * 5000]}]},
        ]
    }
    import json

    text = docs.notebook_to_text(json.dumps(notebook))
    assert text.startswith("## Test Route Policies\nEvaluates a route against a policy.")
    assert '```python\nresult = bf.q.testRoutePolicies(policies="OUT", direction="out").answer().frame()\n```\nOutput:\nNode  Action\nedge1 PERMIT' in text
    assert "<table>" not in text
    assert text.count("Z") == docs.NOTEBOOK_OUTPUT_CHARS


def test_keyword_query_keeps_command_names_and_values():
    assert docs.keyword_query("as-path-expand or as-path-prepend order 65535:666?") == (
        "as-path-expand or as-path-prepend or order or 65535:666"
    )
    assert docs.keyword_query("  ?? ") is None


def test_fusion_rewards_chunks_found_both_ways():
    from langchain_core.documents import Document

    a, b, c, d = (Document(id=x, page_content=x) for x in "abcd")
    # b is second by meaning and first by keyword, so it beats a, which only meaning found.
    fused = docs.fuse([[a, b, c], [b, d]], limit=3)
    assert [doc.id for doc in fused] == ["b", "a", "d"]


def test_interleave_keeps_meaning_searchs_best_and_adds_keyword_finds():
    from langchain_core.documents import Document

    meaning = [Document(id=x, page_content=x) for x in "abcde"]
    keyword = [Document(id=x, page_content=x) for x in "bxy"]
    assert [d.id for d in docs.interleave(meaning, keyword)] == ["a", "b", "c", "x", "y"]
