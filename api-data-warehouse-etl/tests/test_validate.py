"""Validation tests: required fields, nulls, types, duplicates, ranges, references, rejects."""
import pandas as pd

from src import validate as v


def test_clean_payloads_all_valid(payloads):
    res = v.validate_all(payloads)
    assert res["products"].rejected_count == 0
    assert res["users"].rejected_count == 0
    assert res["carts"].rejected_count == 0
    assert res["cart_items"].valid_count == 5
    assert res["cart_items"].rejected_count == 0


def test_missing_required_field_is_rejected(products):
    del products[0]["title"]
    res = v.validate_products(products)
    assert res.valid_count == 2
    assert "missing_required:title" in res.rejected.iloc[0]["reject_reason"]


def test_null_and_blank_values_are_rejected(products):
    products[0]["price"] = None
    products[1]["category"] = "   "
    res = v.validate_products(products)
    assert res.valid_count == 1
    reasons = " | ".join(res.rejected["reject_reason"])
    assert "missing_required:price" in reasons and "missing_required:category" in reasons


def test_wrong_data_types_are_rejected(products):
    products[0]["price"] = "not-a-number"
    products[1]["id"] = "abc"
    res = v.validate_products(products)
    reasons = " | ".join(res.rejected["reject_reason"])
    assert "invalid_type:price" in reasons and "invalid_type:id" in reasons


def test_numeric_strings_are_accepted_as_coercible(products):
    products[0]["price"] = "19.99"
    assert v.validate_products(products).valid_count == 3


def test_range_rules(products):
    products[0]["price"] = -5
    products[1]["rating"]["rate"] = 7
    res = v.validate_products(products)
    assert res.valid_count == 1
    assert res.rejected_count == 2


def test_duplicate_ids_keep_first_reject_rest(products):
    products.append({**products[0], "title": "Duplicate"})
    res = v.validate_products(products)
    assert res.valid_count == 3
    assert res.rejected_count == 1
    assert res.rejected.iloc[0]["reject_reason"] == "duplicate_key:id"


def test_non_object_records_are_rejected(products):
    products.extend(["junk", 42, None])
    res = v.validate_products(products)
    assert res.rejected_count == 3
    assert set(res.rejected["reject_reason"]) == {"record_not_an_object"}
    assert res.valid_count + res.rejected_count == res.input_count


def test_invalid_email_rejected(users):
    users[0]["email"] = "not-an-email"
    res = v.validate_users(users)
    assert res.valid_count == 1
    assert "invalid_format:email" in res.rejected.iloc[0]["reject_reason"]


def test_flattened_nested_user_columns(users):
    res = v.validate_users(users)
    assert {"name_firstname", "address_geolocation_lat"} <= set(res.valid.columns)


def test_cart_without_products_is_rejected(carts):
    carts[0]["products"] = []
    carts, items = v.validate_carts(carts)
    assert carts.rejected_count == 1
    assert "missing_required:products" in carts.rejected.iloc[0]["reject_reason"]
    assert items.valid_count == 3


def test_cart_bad_date_is_rejected(carts):
    carts[0]["date"] = "yesterday"
    res, _ = v.validate_carts(carts)
    assert "invalid_type:date" in res.rejected.iloc[0]["reject_reason"]


def test_cart_line_quantity_and_duplicate_rules(carts):
    carts[0]["products"] = [
        {"productId": 1, "quantity": 0},
        {"productId": 2, "quantity": 1},
        {"productId": 2, "quantity": 5},
    ]
    _, items = v.validate_carts(carts)
    reasons = list(items.rejected["reject_reason"])
    assert any("quantity" in r for r in reasons)
    assert "duplicate_key:cart_id+product_id" in reasons


def test_malformed_cart_line_object_rejected(carts):
    carts[0]["products"] = ["oops", {"productId": 1, "quantity": 1}]
    _, items = v.validate_carts(carts)
    assert items.rejected_count == 1


def test_referential_check_moves_orphans_to_rejected(payloads):
    payloads["carts"][0]["products"].append({"productId": 999, "quantity": 1})
    payloads["carts"][1]["userId"] = 77
    res = v.validate_all(payloads)
    reasons = " | ".join(res["cart_items"].rejected["reject_reason"])
    assert "orphan_reference:product_id" in reasons
    assert "orphan_reference:user_id" in reasons
    items = res["cart_items"]
    assert items.valid_count + items.rejected_count == items.input_count


def test_counts_reconcile_for_every_entity(payloads):
    payloads["products"].append({"id": 1})
    payloads["users"].append("bad")
    for res in v.validate_all(payloads).values():
        assert res.valid_count + res.rejected_count == res.input_count


def test_write_rejected_creates_csv(tmp_path, products):
    products[0]["price"] = -1
    results = {"products": v.validate_products(products)}
    paths = v.write_rejected(results, tmp_path, "run-9")
    out = pd.read_csv(paths["products"])
    assert list(out.columns) == ["run_id", "entity", "reject_reason", "raw_record"]
    assert out.loc[0, "run_id"] == "run-9"


def test_write_rejected_skips_clean_entities(tmp_path, products):
    assert v.write_rejected({"products": v.validate_products(products)}, tmp_path, "r") == {}
