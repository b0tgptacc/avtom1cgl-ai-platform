from decimal import Decimal

import pytest

from calculator import InvoiceTotals, InvalidQuantityError
from calculator.invoice import (
    InvalidCurrencyError,
    InvalidDiscountPercentError,
    InvalidUnitPriceError,
    InvalidVatPercentError,
    calculate_invoice,
)


def test_calculate_invoice_exposes_named_amounts_and_currency():
    invoice = calculate_invoice(
        quantity=Decimal("3"),
        unit_price=Decimal("19.99"),
        discount_percent=Decimal("10"),
        vat_percent=Decimal("20"),
        currency="EUR",
    )

    assert isinstance(invoice, InvoiceTotals)
    assert invoice.gross_amount == Decimal("59.97")
    assert invoice.discount_amount == Decimal("6.00")
    assert invoice.net_amount == Decimal("53.97")
    assert invoice.vat_amount == Decimal("10.79")
    assert invoice.total_amount == Decimal("64.76")
    assert invoice.currency == "EUR"


def test_calculate_invoice_uses_round_half_up_for_each_named_money_value():
    invoice = calculate_invoice(
        quantity=Decimal("1"),
        unit_price=Decimal("10.005"),
        discount_percent=Decimal("10"),
        vat_percent=Decimal("10"),
        currency="USD",
    )

    assert invoice.gross_amount == Decimal("10.01")
    assert invoice.discount_amount == Decimal("1.00")
    assert invoice.net_amount == Decimal("9.01")
    assert invoice.vat_amount == Decimal("0.90")
    assert invoice.total_amount == Decimal("9.91")


def test_calculate_invoice_rejects_discount_over_one_hundred_percent():
    with pytest.raises(InvalidDiscountPercentError):
        calculate_invoice(
            quantity=Decimal("1"),
            unit_price=Decimal("10"),
            discount_percent=Decimal("100.01"),
            vat_percent=Decimal("20"),
            currency="USD",
        )


@pytest.mark.parametrize(
    ("argument", "value", "error_type"),
    [
        ("quantity", Decimal("0"), InvalidQuantityError),
        ("unit_price", Decimal("-0.01"), InvalidUnitPriceError),
        ("vat_percent", Decimal("-0.01"), InvalidVatPercentError),
        ("currency", "", InvalidCurrencyError),
    ],
)
def test_calculate_invoice_raises_named_errors_for_invalid_inputs(
    argument, value, error_type
):
    arguments = {
        "quantity": Decimal("1"),
        "unit_price": Decimal("10"),
        "discount_percent": Decimal("0"),
        "vat_percent": Decimal("20"),
        "currency": "USD",
    }
    arguments[argument] = value

    with pytest.raises(error_type):
        calculate_invoice(**arguments)
