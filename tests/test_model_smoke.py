import pytest


@pytest.mark.model
def test_real_model_ranks_by_meaning():
    from folder_search.engine.model import Embedder

    e = Embedder()
    docs = e.embed_documents(
        [
            "The claimant first knew the injury was significant in 2019.",
            "The tenancy deposit was not protected within 30 days.",
        ]
    )
    q = e.embed_queries(["when did the limitation period start to run"])[0]
    sims = docs @ q
    assert sims[0] > sims[1]
