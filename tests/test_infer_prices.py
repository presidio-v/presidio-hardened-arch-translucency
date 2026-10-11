"""Tests for dated GPU price catalogs (infer_prices.py) and their CLI use.

Every price here is a fixture: provider ``example``, sources on
``example.invalid`` (RFC 2606). pat ships no real prices.
"""

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.infer_prices import (
    CATALOG_SCHEMA,
    COMMITMENT_KEY,
    PriceCatalogError,
    PriceCatalogTamperError,
    catalog_age,
    catalog_digest,
    catalog_template,
    commit_price_catalog,
    layout_price,
    load_price_catalog,
    require_gpu_match,
    select_family,
)

runner = CliRunner()
TODAY = datetime.now(timezone.utc).date()


def _entry(sku, gpus, price, **kw):
    entry = {
        "provider": "example",
        "region": {"country": "FR", "datacenter": "par-1"},
        "gpu": {"model": "L40S", "memory_gb": 48, "bandwidth_gbs": 864},
        "sku": sku,
        "gpus_per_instance": gpus,
        "billing": {"basis": "instance", "unit": "hour", "price": price},
        "tier": "on-demand",
        "source_url": "https://example.invalid/pricing",
        "retrieved_at": f"{kw.pop('retrieved', TODAY.isoformat())}T00:00:00Z",
        "valid_from": "2026-01-01",
    }
    entry.update(kw)
    return entry


def _catalog(entries=None, as_of=None):
    as_of = as_of or TODAY
    return {
        "schema": CATALOG_SCHEMA,
        "as_of": as_of.isoformat(),
        "currency": "EUR",
        "vat": "excluded",
        "entries": entries
        or [
            _entry("L40S-1", 1, "1.40", retrieved=as_of.isoformat()),
            _entry("L40S-2", 2, "2.60", retrieved=as_of.isoformat()),
            _entry("L40S-8", 8, "11.00", retrieved=as_of.isoformat()),
        ],
    }


def _committed(tmp_path, data=None, name="prices.json"):
    data = data if data is not None else _catalog()
    source = tmp_path / f"draft-{name}"
    source.write_text(json.dumps(data))
    target = tmp_path / name
    commit_price_catalog(source, target, today=TODAY)
    return target


def _write(tmp_path, data, name="raw.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# Commitment
# ---------------------------------------------------------------------------


def test_commit_round_trip_and_digest(tmp_path):
    path = _committed(tmp_path)
    record = json.loads(path.read_text())
    assert record[COMMITMENT_KEY]["digest"] == catalog_digest(record)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    catalog = load_price_catalog(path, today=TODAY)
    assert catalog.name == "prices.json" and len(catalog.entries) == 3
    assert catalog.digest == record[COMMITMENT_KEY]["digest"]
    assert catalog.as_of == TODAY


def test_any_edit_after_commit_is_tamper(tmp_path):
    path = _committed(tmp_path)
    record = json.loads(path.read_text())
    record["entries"][0]["billing"]["price"] = "1.41"
    path.write_text(json.dumps(record))
    with pytest.raises(PriceCatalogTamperError, match="modified"):
        load_price_catalog(path, today=TODAY)


def test_uncommitted_catalogs_do_not_load_and_commits_are_not_reblessed(tmp_path):
    raw = _write(tmp_path, _catalog())
    with pytest.raises(PriceCatalogTamperError, match="no valid commitment"):
        load_price_catalog(raw, today=TODAY)
    committed = _committed(tmp_path)
    with pytest.raises(PriceCatalogError, match="already committed"):
        commit_price_catalog(committed, tmp_path / "again.json", today=TODAY)
    with pytest.raises(FileExistsError):
        commit_price_catalog(raw, committed, today=TODAY)


def test_template_can_never_load(tmp_path):
    with pytest.raises(PriceCatalogError):
        commit_price_catalog(_write(tmp_path, catalog_template()), tmp_path / "t.json")


# ---------------------------------------------------------------------------
# Strict content
# ---------------------------------------------------------------------------


def _with(change):
    data = _catalog()
    change(data)
    return data


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (_with(lambda d: d.update(extra=1)), "unknown catalog keys"),
        (_with(lambda d: d["entries"][0].update(egress="0.01")), "unknown entry"),
        (_with(lambda d: d.update(currency="USD")), "EUR"),
        (_with(lambda d: d.update(vat="included")), "VAT"),
        (_with(lambda d: d.update(schema="x@1")), "schema"),
        (_with(lambda d: d["entries"][0]["billing"].update(price=1.4)), "decimal"),
        (_with(lambda d: d["entries"][0]["billing"].update(price="1,40")), "decimal"),
        (_with(lambda d: d["entries"][0]["billing"].update(price="0.01")), "outside"),
        (_with(lambda d: d["entries"][0]["billing"].update(basis="node")), "basis"),
        (_with(lambda d: d["entries"][0].update(tier="reserved")), "tier"),
        (_with(lambda d: d["entries"][0].update(tier="committed")), "commitment"),
        (
            _with(lambda d: d["entries"][0].update(commitment={"term_months": 12})),
            "commitment",
        ),
        (_with(lambda d: d["entries"][0].update(provider="Example")), "slug"),
        (_with(lambda d: d["entries"][0]["region"].update(country="fr")), "ISO"),
        (
            _with(lambda d: d["entries"][0].update(source_url="http://x.invalid/")),
            "https",
        ),
        (
            _with(lambda d: d["entries"][0].update(source_url="https://u:p@x.invalid")),
            "credentials",
        ),
        (_with(lambda d: d["entries"][0].update(sku="[red]x\x1b")), "printable"),
        (_with(lambda d: d["entries"][0].update(gpus_per_instance=0)), "integer"),
        (_with(lambda d: d["entries"][0].pop("source_url")), "missing"),
        (_with(lambda d: d["entries"][1].update(sku="L40S-1")), "duplicate SKU"),
        (_with(lambda d: d.update(entries=[])), "entries"),
        (_with(lambda d: d.update(as_of="2026-13-01")), "valid date"),
        (
            _with(lambda d: d.update(as_of=(TODAY + timedelta(days=3)).isoformat())),
            "future",
        ),
        (
            _with(
                lambda d: d["entries"][0].update(
                    retrieved_at=f"{TODAY + timedelta(days=1)}T00:00:00Z"
                )
            ),
            "retrieved_at",
        ),
        (
            _with(
                lambda d: d["entries"][0].update(
                    retrieved_at=f"{TODAY - timedelta(days=40)}T00:00:00Z"
                )
            ),
            "retrieved_at",
        ),
        (
            _with(
                lambda d: d["entries"][0].update(
                    valid_from=(TODAY + timedelta(days=1)).isoformat()
                )
            ),
            "valid_from",
        ),
    ],
)
def test_malformed_catalogs_are_refused(tmp_path, data, message):
    with pytest.raises(PriceCatalogError, match=message):
        commit_price_catalog(_write(tmp_path, data), tmp_path / "out.json", today=TODAY)
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize(
    "text",
    [
        '{"schema": 1, "schema": 2}',
        '{"as_of": NaN}',
        "[1]",
        "not json",
    ],
)
def test_json_hygiene(tmp_path, text):
    path = tmp_path / "c.json"
    path.write_text(text)
    with pytest.raises(PriceCatalogError):
        commit_price_catalog(path, tmp_path / "out.json", today=TODAY)


def test_file_hygiene(tmp_path):
    real = _committed(tmp_path)
    link = tmp_path / "link.json"
    os.symlink(real, link)
    with pytest.raises(PriceCatalogError, match="symbolic link"):
        load_price_catalog(link, today=TODAY)
    big = tmp_path / "big.json"
    big.write_text(" " * 1_048_577)
    with pytest.raises(PriceCatalogError, match="exceeds"):
        load_price_catalog(big, today=TODAY)
    with pytest.raises(PriceCatalogError, match="could not be read"):
        load_price_catalog(tmp_path / "missing.json", today=TODAY)


def test_unit_and_basis_normalise_to_eur_per_gpu_hour(tmp_path):
    entries = [
        _entry("per-gpu", 4, "1.50", billing={"basis": "gpu", "unit": "hour",
               "price": "1.50"}),
        _entry("per-minute", 2, "0.05", billing={"basis": "instance",
               "unit": "minute", "price": "0.05"}),
    ]  # fmt: skip
    catalog = load_price_catalog(_committed(tmp_path, _catalog(entries)), today=TODAY)
    by_sku = {e.sku: e for e in catalog.entries}
    assert by_sku["per-gpu"].eur_per_instance_hour == Decimal("6.00")
    assert by_sku["per-gpu"].eur_per_gpu_hour == Decimal("1.50")
    assert by_sku["per-minute"].eur_per_instance_hour == Decimal("3.00")
    assert by_sku["per-minute"].eur_per_gpu_hour == Decimal("1.50")


# ---------------------------------------------------------------------------
# Freshness, selection, hardware, packing
# ---------------------------------------------------------------------------


def test_cited_prices_are_plain_decimals(tmp_path):
    entries = [_entry("ten", 1, "10.00"), _entry("twenty", 2, "20")]
    catalog = load_price_catalog(_committed(tmp_path, _catalog(entries)), today=TODAY)
    family = select_family(catalog, "example")
    assert family.head.cited()["eur_per_gpu_hour"] == "10"
    assert layout_price(family, 1, 4).as_dict()["eur_per_hour"] == "40"


def test_retrieval_window_is_thirty_days_and_never_future(tmp_path):
    def commit(retrieved, now=None, name="w.json"):
        data = _catalog([_entry("a", 1, "1.40", retrieved_at=retrieved)])
        return commit_price_catalog(
            _write(tmp_path, data, f"in-{name}"), tmp_path / name, today=TODAY, now=now
        )

    commit(f"{TODAY - timedelta(days=30)}T00:00:00Z", name="edge.json")
    with pytest.raises(PriceCatalogError, match="between"):
        commit(f"{TODAY - timedelta(days=31)}T23:59:59Z", name="early.json")
    morning = datetime.combine(TODAY, datetime.min.time(), timezone.utc)
    with pytest.raises(PriceCatalogError, match="in the future"):
        commit(f"{TODAY}T09:00:00Z", now=morning + timedelta(hours=8), name="f.json")


def test_hardware_gate_checks_every_sku_of_the_family(tmp_path):
    entries = [
        _entry("a", 1, "1.40", gpu={"model": "L40S", "memory_gb": 48}),
        _entry("b", 2, "2.60", gpu={"model": "L40S", "memory_gb": 48,
                                    "bandwidth_gbs": 3350}),
    ]  # fmt: skip
    catalog = load_price_catalog(_committed(tmp_path, _catalog(entries)), today=TODAY)
    # Different declared bandwidth: two families, never one silently matched.
    with pytest.raises(PriceCatalogError, match="several price families"):
        select_family(catalog, "example")


def test_staleness_windows(tmp_path):
    catalog = load_price_catalog(_committed(tmp_path), today=TODAY)
    assert catalog_age(catalog, TODAY + timedelta(days=30)) == (30, False)
    assert catalog_age(catalog, TODAY + timedelta(days=31)) == (31, True)
    assert catalog_age(catalog, TODAY + timedelta(days=90)) == (90, True)
    with pytest.raises(PriceCatalogError, match="91 days old"):
        catalog_age(catalog, TODAY + timedelta(days=91))


def _family_catalog(tmp_path):
    entries = copy.deepcopy(_catalog()["entries"]) + [
        _entry("L40S-1-de", 1, "1.30", region={"country": "DE"}),
        _entry("H100-1", 1, "3.00", gpu={"model": "H100", "memory_gb": 80}),
        _entry("L40S-1-spot", 1, "0.70", tier="spot"),
        _entry("L40S-2-c", 2, "2.00", tier="committed",
               commitment={"term_months": 12, "min_instances": 2}),
    ]  # fmt: skip
    return load_price_catalog(_committed(tmp_path, _catalog(entries)), today=TODAY)


def test_selection_must_resolve_to_one_family(tmp_path):
    catalog = _family_catalog(tmp_path)
    with pytest.raises(PriceCatalogError, match="several price families") as info:
        select_family(catalog, "example")
    assert "FR/par-1 L40S" in str(info.value) and "DE L40S" in str(info.value)
    family = select_family(catalog, "example", region="par-1", gpu="l40s")
    assert [e.sku for e in family.entries] == ["L40S-1", "L40S-2", "L40S-8"]
    assert select_family(catalog, "example", tier="spot").head.sku == "L40S-1-spot"
    with pytest.raises(PriceCatalogError, match="no on-demand prices"):
        select_family(catalog, "other")
    with pytest.raises(PriceCatalogError, match="tier"):
        select_family(catalog, "example", tier="reserved")


def test_priced_gpu_must_be_the_analysed_gpu(tmp_path):
    family = select_family(
        _family_catalog(tmp_path), "example", region="par-1", gpu="L40S"
    )
    require_gpu_match(family, 48, 864, gpu_type="L40S", gpus_per_node=8)
    with pytest.raises(PriceCatalogError, match="GPU memory"):
        require_gpu_match(family, 80, 864)
    with pytest.raises(PriceCatalogError, match="bandwidth"):
        require_gpu_match(family, 48, 3350)
    with pytest.raises(PriceCatalogError, match="calibration is for GPU"):
        require_gpu_match(family, 48, 864, gpu_type="H100")
    with pytest.raises(PriceCatalogError, match="--gpus-per-node 8"):
        require_gpu_match(family, 48, 864, gpus_per_node=4)


def test_layout_price_packs_instances(tmp_path):
    family = select_family(
        _family_catalog(tmp_path), "example", region="par-1", gpu="L40S"
    )
    # Two replicas on two GPUs: one 2-GPU instance beats two 1-GPU instances.
    replicas = layout_price(family, 1, 2)
    assert (replicas.entry.sku, replicas.eur_per_hour) == ("L40S-2", Decimal("2.60"))
    assert layout_price(family, 1, 1).eur_per_hour == Decimal("1.40")
    tensor = layout_price(family, 2, 1)
    assert (tensor.entry.sku, tensor.gpus_billed) == ("L40S-2", 2)
    # tp4 only fits the 8-GPU instance: eight GPUs billed for four used.
    tp4 = layout_price(family, 4, 1)
    assert (tp4.entry.sku, tp4.gpus_billed, tp4.eur_per_hour) == (
        "L40S-8",
        8,
        Decimal("11.00"),
    )
    assert layout_price(family, 4, 2).instances_billed == 1
    missing = layout_price(family, 16, 1)
    assert missing.eur_per_hour is None and "tp=16" in missing.reason
    assert missing.as_dict()["sku"] is None


def test_committed_tier_bills_its_minimum(tmp_path):
    family = select_family(_family_catalog(tmp_path), "example", tier="committed")
    price = layout_price(family, 2, 1)
    assert price.instances_billed == 2 and price.eur_per_hour == Decimal("4.00")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_COMMON = [
    "-r", "5", "-p", "1000", "-o", "256", "-w", "16", "-k", "131072",
    "-m", "48", "-b", "864",
]  # fmt: skip


def _invoke(*args):
    return runner.invoke(app, ["--skip-audit", *args])


def _priced(path, *extra):
    return (
        "--price-catalog", str(path), "--price-provider", "example", *extra
    )  # fmt: skip


def test_cli_analyze_prices_layouts_and_cites_the_catalog(tmp_path):
    path = _committed(tmp_path)
    out = _invoke("infer-analyze", *_COMMON, "-n", "2", *_priced(path), "--json")
    assert out.exit_code == 0, out.output
    data = json.loads(out.stdout)
    catalog = data["price_catalog"]
    assert catalog["digest"] == json.loads(path.read_text())[COMMITMENT_KEY]["digest"]
    assert catalog["stale"] is False and catalog["currency"] == "EUR"
    assert catalog["selection"]["gpu"] == "L40S"
    assert {e["sku"] for e in catalog["entries_used"]} <= {"L40S-1", "L40S-2"}
    assert all(e["source_url"].startswith("https://") for e in catalog["entries_used"])
    rows = {(c["tp"], c["instances"]): c for c in data["configs"]}
    assert rows[(1, 2)]["price"]["sku"] == "L40S-2"
    assert rows[(1, 2)]["cost_per_hour"] == pytest.approx(2.60)
    assert rows[(1, 1)]["cost_per_hour"] == pytest.approx(1.40)
    assert data["cheapest"] is not None

    table = _invoke("infer-analyze", *_COMMON, "-n", "2", *_priced(path))
    assert table.exit_code == 0, table.output
    flat = " ".join(table.output.split())
    assert "€/M out" in flat and "Prices (EUR, VAT excluded)" in flat
    assert "Cheapest meeting the SLO" in flat


def test_cli_uniform_price_output_is_unchanged(tmp_path):
    out = _invoke(
        "infer-analyze", *_COMMON, "-n", "2", "--cost-per-gpu-hour", "1.2", "--json"
    )
    assert out.exit_code == 0, out.output
    data = json.loads(out.stdout)
    assert "price_catalog" not in data and "cheapest" not in data
    assert "price" not in data["configs"][0]


def test_cli_what_if_shows_the_billed_instances(tmp_path):
    path = _committed(tmp_path)
    out = _invoke("infer-what-if", "--tp", "1", "-i", "2", *_COMMON, *_priced(path))
    assert out.exit_code == 0, out.output
    flat = " ".join(out.output.split())
    assert "1× L40S-2 (2 GPUs)" in flat and "€/h" in flat
    data = json.loads(
        _invoke(
            "infer-what-if", "--tp", "1", "-i", "2", *_COMMON, *_priced(path), "--json"
        ).stdout
    )
    assert data["price"]["sku"] == "L40S-2" and data["price_catalog"]["as_of"]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--cost-per-gpu-hour", "1"), "cannot be combined"),
        (("--price-gpu", "H100"), "no on-demand prices"),
        (("--gpus-per-node", "4"), "--gpus-per-node 8"),
    ],
)
def test_cli_pricing_refusals(tmp_path, extra, message):
    path = _committed(tmp_path)
    out = _invoke("infer-analyze", *_COMMON, "-n", "2", *_priced(path), *extra)
    assert out.exit_code == 2
    assert message in " ".join(out.output.split())


def test_cli_price_options_need_a_catalog_and_a_provider(tmp_path):
    out = _invoke("infer-analyze", *_COMMON, "-n", "2", "--price-provider", "x")
    assert out.exit_code == 2 and "need --price-catalog" in out.output
    path = _committed(tmp_path)
    out = _invoke("infer-analyze", *_COMMON, "-n", "2", "--price-catalog", str(path))
    assert out.exit_code == 2 and "--price-provider" in out.output


def test_cli_stale_catalog_warns_and_old_catalog_is_refused(tmp_path):
    def committed(age, name):
        as_of = TODAY - timedelta(days=age)
        data = _catalog(
            [_entry("L40S-1", 1, "1.40", retrieved=as_of.isoformat())], as_of=as_of
        )
        return _committed(tmp_path, data, name=name)

    stale = _invoke("infer-analyze", *_COMMON, "-n", "1",
                    *_priced(committed(45, "old.json")), "--json")  # fmt: skip
    assert stale.exit_code == 0, stale.output
    catalog = json.loads(stale.stdout)["price_catalog"]
    # The CLI reads the real clock: a run may cross UTC midnight.
    assert catalog["stale"] is True and catalog["age_days"] in (45, 46)
    assert "days old" in stale.output
    refused = _invoke("infer-analyze", *_COMMON, "-n", "1",
                      *_priced(committed(120, "ancient.json")))  # fmt: skip
    assert refused.exit_code == 2 and "120 days old" in refused.output


def test_cli_tampered_catalog_is_refused(tmp_path):
    path = _committed(tmp_path)
    record = json.loads(path.read_text())
    record["as_of"] = (TODAY - timedelta(days=1)).isoformat()
    path.write_text(json.dumps(record))
    out = _invoke("infer-analyze", *_COMMON, "-n", "1", *_priced(path))
    assert out.exit_code == 2 and "commitment" in out.output


def test_cli_infer_prices_list_commit_and_template(tmp_path):
    draft = _write(tmp_path, _catalog(), "draft.json")
    target = tmp_path / "committed.json"
    out = _invoke("infer-prices", str(draft), "--commit", str(target))
    assert out.exit_code == 0, out.output
    assert target.exists()
    again = _invoke("infer-prices", str(draft), "--commit", str(target))
    assert again.exit_code == 2 and "already exists" in again.output

    listing = _invoke("infer-prices", str(target))
    assert listing.exit_code == 0, listing.output
    flat = " ".join(listing.output.split())
    assert "L40S-8" in flat and "11.00" in flat and "1.38" in flat  # €/GPU-h

    data = json.loads(
        _invoke("infer-prices", str(target), "--json", "--gpu", "l40s").stdout
    )
    assert data["vat"] == "excluded" and len(data["entries"]) == 3
    one = json.loads(
        _invoke("infer-prices", str(target), "--json", "--provider", "none").stdout
    )
    assert one["entries"] == []

    template = _invoke("infer-prices", "--template")
    assert template.exit_code == 0
    assert "price" not in json.loads(template.stdout)["entries"][0]["billing"]
    assert _invoke("infer-prices").exit_code == 2
    assert _invoke("infer-prices", str(target), "--tier", "x").exit_code == 2
    assert _invoke("infer-prices", str(draft)).exit_code == 2  # uncommitted
    mixed = _invoke("infer-prices", str(draft), "--commit", "x.json", "--json")
    assert mixed.exit_code == 2 and "does not take --json" in mixed.output
    assert _invoke("infer-prices", str(target), "--template").exit_code == 2


def test_catalog_labels_cannot_inject_markup(tmp_path):
    entries = [_entry("x[/]", 1, "1.40"), _entry("x[/]", 1, "1.50")]
    draft = _write(tmp_path, _catalog(entries), "draft.json")
    out = _invoke("infer-prices", str(draft), "--commit", str(tmp_path / "o.json"))
    assert out.exit_code == 2 and "x[/]" in out.output
    styled = [
        _entry("a", 1, "1.40", gpu={"model": "[bold red]L40S[/]", "memory_gb": 48}),
        _entry("b", 1, "1.40", region={"country": "DE"},
               gpu={"model": "[bold red]L40S[/]", "memory_gb": 48}),
    ]  # fmt: skip
    path = _committed(tmp_path, _catalog(styled), name="styled.json")
    out = _invoke("infer-analyze", *_COMMON, "-n", "1", *_priced(path))
    assert out.exit_code == 2 and "[bold red]L40S[/]" in out.output
