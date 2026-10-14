from chatbot.config import SAMPLE_DOCS_DIR
from chatbot.retrieval import AzureSearchRetriever, LocalRetriever, build_search_query, split_text


def test_full_question_is_searched_alone():
    history = [{"role": "user", "content": "warranty for fridges"}]
    q = "How long does the standard warranty last for washing machines?"
    assert build_search_query(q, history) == q


def test_short_follow_up_includes_previous_user_question():
    history = [{"role": "user", "content": "What does the  warranty cover?"},
               {"role": "assistant", "content": "It covers defects [1]."}]
    assert build_search_query("and seals?", history) == "What does the warranty cover? and seals?"


def test_follow_up_without_history():
    assert build_search_query("refunds?", []) == "refunds?"


def test_query_is_capped_keeping_the_end():
    q = build_search_query("x" * 900, [], max_chars=500)
    assert len(q) == 500


def test_split_text_overlaps():
    chunks = split_text("abcdefghij" * 50, size=200, overlap=50)
    assert all(len(c) <= 200 for c in chunks)
    assert chunks[0][-50:] == chunks[1][:50]
    assert "".join(c[:150] for c in chunks[:-1]) + chunks[-1] == "abcdefghij" * 50
    assert split_text("   ") == []


class FakeSearch:
    def __init__(self, docs):
        self.docs, self.calls = docs, []

    def search(self, text, top):
        self.calls.append((text, top))
        return self.docs


def test_azure_retriever_maps_results_and_skips_empty_chunks():
    fake = FakeSearch([
        {"chunk_id": "c1", "title": "a.pdf", "chunk": " text ", "@search.score": 0.5, "@search.rerankerScore": 2.9},
        {"chunk_id": "c2", "title": None, "chunk": "more", "@search.score": 0.4},
        {"chunk_id": "c3", "title": "empty.pdf", "chunk": ""},
    ])
    result = AzureSearchRetriever(fake).retrieve("q", 5)
    assert fake.calls == [("q", 5)]
    assert [(p.id, p.title, p.content, p.score) for p in result] == [
        ("c1", "a.pdf", "text", 2.9), ("c2", "Untitled document", "more", 0.4)]


def test_local_retriever_finds_sample_document(tmp_path):
    (tmp_path / "notes.txt").write_text("Nothing about appliances here.")
    (tmp_path / "ignored.pdf").write_bytes(b"%PDF")
    hits = LocalRetriever([SAMPLE_DOCS_DIR, tmp_path, tmp_path / "missing"]).retrieve("refund cash on delivery", 3)
    assert hits and hits[0].title == "returns-and-refunds.md"
    assert LocalRetriever([SAMPLE_DOCS_DIR]).retrieve("the and of", 3) == []
