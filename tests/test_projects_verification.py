

def test_every_verification_transition_honours_a_custom_prefix():
    """A store configured with a non-default verification prefix must keep every
    transition on that prefix -- not read one key and write another."""
    from lazybridge import Store

    from lazytools.projects import verification as v

    store = Store()
    prefix = "custom:verification:"
    assert v.claim_verification(store, contract_id="c1", job_id="j1", prefix=prefix) is not None
    assert v.start_running(store, "j1", prefix=prefix) is not None
    assert v.request_rework(store, "j1", reviewer="r", reason="x", prefix=prefix) is not None
    assert store.read("ceo:verification:j1") is None
    assert store.read(f"{prefix}j1")["status"] == "rework"
