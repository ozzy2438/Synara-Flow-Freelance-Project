from synara.data.generator import generate_world


def test_catalog_is_deterministic_and_has_spikes():
    a, oa = generate_world(seed=42)
    b, ob = generate_world(seed=42)
    assert [s.sku for s in a] == [s.sku for s in b]
    assert len(a) == 50
    spiked = [s for s in a if s.spiked]
    assert len(spiked) == 5
    assert all(s.is_high_runner for s in spiked)
    assert len(oa) == len(ob)
    assert len(oa) > 100
