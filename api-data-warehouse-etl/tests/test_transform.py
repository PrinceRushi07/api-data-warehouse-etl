"""Transformation tests: cleaning, standardisation, derived columns, dimensions, outputs."""
import pandas as pd
import pytest

from src import transform as t
from src import validate as v
from src.transform import EXPECTED_COLUMNS, TransformError


@pytest.fixture
def valid(payloads):
    res = v.validate_all(payloads)
    return {"products": res["products"].valid, "users": res["users"].valid, "cart_items": res["cart_items"].valid}


@pytest.fixture
def frames(valid):
    return t.transform_all(valid)


def test_all_frames_match_column_contract(frames):
    for name, frame in frames.items():
        assert list(frame.columns) == EXPECTED_COLUMNS[name]


def test_product_cleaning_and_standardisation(frames):
    p = frames["dim_product"].set_index("product_id")
    assert p.loc[1, "title"] == "Fjallraven Backpack"           # trimmed
    assert p.loc[1, "category"] == "men's clothing"             # lower-cased
    assert p.loc[1, "category_display"] == "Men's Clothing"     # nice casing
    assert p.loc[1, "category"] == p.loc[2, "category"]         # standardised across spellings


def test_product_derived_columns(frames):
    p = frames["dim_product"].set_index("product_id")
    assert p.loc[1, "price_tier"] == "premium"
    assert p.loc[2, "price_tier"] == "budget"
    assert p.loc[1, "rating_band"] == "average"
    assert p.loc[2, "rating_band"] == "excellent"
    assert p.loc[3, "rating_band"] == "excellent"


def test_customer_standardisation(frames):
    c = frames["dim_customer"].set_index("customer_id")
    assert c.loc[1, "email"] == "john@gmail.com"
    assert c.loc[1, "full_name"] == "John Doe"
    assert c.loc[1, "city"] == "Kilcoole"
    assert c.loc[1, "street_address"] == "7682 New Road"
    assert c.loc[1, "email_domain"] == "gmail.com"
    assert c.loc[1, "latitude"] == pytest.approx(-37.3159)
    assert "password" not in frames["dim_customer"].columns


def test_fact_measures(frames):
    f = frames["fact_sales"].set_index(["cart_id", "product_id"])
    row = f.loc[(1, 1)]
    assert row["unit_price"] == pytest.approx(109.95)
    assert row["quantity"] == 4
    assert row["line_total"] == pytest.approx(439.80)
    assert row["date_key"] == 20200302
    assert len(f) == 5


def test_fact_grain_is_unique(frames):
    assert not frames["fact_sales"].duplicated(["cart_id", "product_id"]).any()


def test_dim_date_covers_full_range_without_gaps(frames):
    d = frames["dim_date"]
    assert d["date_key"].min() == 20200302 and d["date_key"].max() == 20200310
    assert len(d) == 9
    assert d["date_key"].is_unique
    monday = d[d["date_key"] == 20200302].iloc[0]
    assert monday["day_name"] == "Monday" and monday["day_of_week"] == 1 and not monday["is_weekend"]
    saturday = d[d["date_key"] == 20200307].iloc[0]
    assert saturday["is_weekend"]
    assert saturday["year_month"] == "2020-03" and saturday["quarter"] == 1


def test_every_fact_date_exists_in_dim_date(frames):
    assert set(frames["fact_sales"]["date_key"]) <= set(frames["dim_date"]["date_key"])


def test_unknown_product_in_fact_raises(valid):
    valid["cart_items"].loc[0, "product_id"] = 12345
    with pytest.raises(TransformError):
        t.transform_all(valid)


def test_empty_inputs_produce_empty_but_well_formed_frames(valid):
    empty = {k: v_.iloc[0:0] for k, v_ in valid.items()}
    frames = t.transform_all(empty)
    for name, frame in frames.items():
        assert frame.empty and list(frame.columns) == EXPECTED_COLUMNS[name]


def test_write_processed_outputs_csv_per_table(tmp_path, frames):
    paths = t.write_processed(frames, tmp_path)
    assert set(paths) == set(frames)
    for name, path in paths.items():
        assert path.name.startswith(name + "_") and path.suffix == ".csv"
        assert len(pd.read_csv(path)) == len(frames[name])
