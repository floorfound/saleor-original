import json
from decimal import Decimal
from unittest.mock import ANY, patch
from uuid import uuid4

import graphene
import pytest

from ....checkout import calculations
from ....checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ....checkout.models import Checkout
from ....order.models import Order
from ....payment import PaymentError
from ....payment.error_codes import PaymentErrorCode
from ....payment.gateways.dummy_credit_card import (
    TOKEN_EXPIRED,
    TOKEN_VALIDATION_MAPPING,
)
from ....payment.interface import (
    CustomerSource,
    InitializedPaymentResponse,
    PaymentMethodInfo,
    TokenConfig,
)
from ....payment.models import ChargeStatus, Payment, TransactionKind
from ....payment.utils import fetch_customer_id, store_customer_id
from ....plugins.manager import PluginsManager, get_plugins_manager
from ...tests.utils import (
    assert_no_permission,
    get_graphql_content,
    get_graphql_content_from_response,
)
from ..enums import OrderAction, PaymentChargeStatusEnum

DUMMY_GATEWAY = "mirumee.payments.dummy"

VOID_QUERY = """
    mutation PaymentVoid($paymentId: ID!) {
        paymentVoid(paymentId: $paymentId) {
            payment {
                id,
                chargeStatus
            }
            errors {
                field
                message
            }
        }
    }
"""


def test_payment_void_success(
    staff_api_client, permission_manage_orders, payment_txn_preauth
):
    assert payment_txn_preauth.charge_status == ChargeStatus.AUTHORIZED
    payment_id = graphene.Node.to_global_id("Payment", payment_txn_preauth.pk)
    variables = {"paymentId": payment_id}
    response = staff_api_client.post_graphql(
        VOID_QUERY, variables, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    data = content["data"]["paymentVoid"]
    assert not data["errors"]
    payment_txn_preauth.refresh_from_db()
    assert payment_txn_preauth.is_active is False
    assert payment_txn_preauth.transactions.count() == 2
    assert payment_txn_preauth.charge_status == ChargeStatus.CANCELLED
    txn = payment_txn_preauth.transactions.last()
    assert txn.kind == TransactionKind.VOID


def test_payment_void_gateway_error(
    staff_api_client, permission_manage_orders, payment_txn_preauth, monkeypatch
):
    assert payment_txn_preauth.charge_status == ChargeStatus.AUTHORIZED
    payment_id = graphene.Node.to_global_id("Payment", payment_txn_preauth.pk)
    variables = {"paymentId": payment_id}
    monkeypatch.setattr("saleor.payment.gateways.dummy.dummy_success", lambda: False)
    response = staff_api_client.post_graphql(
        VOID_QUERY, variables, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    data = content["data"]["paymentVoid"]
    assert data["errors"]
    assert data["errors"][0]["field"] is None
    assert data["errors"][0]["message"] == "Unable to void the transaction."
    payment_txn_preauth.refresh_from_db()
    assert payment_txn_preauth.charge_status == ChargeStatus.AUTHORIZED
    assert payment_txn_preauth.is_active is True
    assert payment_txn_preauth.transactions.count() == 2
    txn = payment_txn_preauth.transactions.last()
    assert txn.kind == TransactionKind.VOID
    assert not txn.is_success


CREATE_PAYMENT_MUTATION = """
    mutation CheckoutPaymentCreate($token: UUID, $input: PaymentInput!) {
        checkoutPaymentCreate(token: $token, input: $input) {
            payment {
                transactions {
                    kind,
                    token
                }
                chargeStatus
            }
            errors {
                code
                field
            }
        }
    }
    """


@pytest.fixture
def create_payment_input():
    def _factory(
        checkout, amount=None, partial=None, return_url=None, token="sample-token"
    ):
        payload = {
            "token": checkout.token,
            "input": {
                "gateway": DUMMY_GATEWAY,
                "token": token,
            },
        }
        if amount is not None:
            payload["input"]["amount"] = str(amount)
        if partial is not None:
            payload["input"]["partial"] = partial
        if return_url is not None:
            payload["input"]["returnUrl"] = return_url
        return payload

    return _factory


def test_checkout_add_payment_without_shipping_method_and_not_shipping_required(
    user_api_client, checkout_without_shipping_required, address, create_payment_input
):
    checkout = checkout_without_shipping_required
    checkout.billing_address = address
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )
    variables = create_payment_input(checkout, total.gross.amount)
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentCreate"]
    assert not data["errors"]
    transactions = data["payment"]["transactions"]
    assert not transactions
    payment = Payment.objects.get()
    assert payment.checkout == checkout
    assert payment.is_active
    assert payment.token == "sample-token"
    assert payment.total == total.gross.amount
    assert payment.currency == total.gross.currency
    assert payment.charge_status == ChargeStatus.NOT_CHARGED
    assert payment.billing_address_1 == checkout.billing_address.street_address_1
    assert payment.billing_first_name == checkout.billing_address.first_name
    assert payment.billing_last_name == checkout.billing_address.last_name


def test_checkout_add_payment_without_shipping_method_with_shipping_required(
    user_api_client, checkout_with_shipping_required, address, create_payment_input
):
    checkout = checkout_with_shipping_required

    checkout.billing_address = address
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )
    variables = create_payment_input(checkout, total.gross.amount)
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentCreate"]

    assert data["errors"][0]["code"] == "SHIPPING_METHOD_NOT_SET"
    assert data["errors"][0]["field"] == "shippingMethod"


def test_checkout_add_payment_with_shipping_method_and_shipping_required(
    user_api_client,
    checkout_with_shipping_required,
    other_shipping_method,
    address,
    create_payment_input,
):
    checkout = checkout_with_shipping_required
    checkout.billing_address = address
    checkout.shipping_address = address
    checkout.shipping_method = other_shipping_method
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )
    variables = create_payment_input(checkout, total.gross.amount)
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentCreate"]

    assert not data["errors"]
    transactions = data["payment"]["transactions"]
    assert not transactions
    payment = Payment.objects.get()
    assert payment.checkout == checkout
    assert payment.is_active
    assert payment.token == "sample-token"
    assert payment.total == total.gross.amount
    assert payment.currency == total.gross.currency
    assert payment.charge_status == ChargeStatus.NOT_CHARGED
    assert payment.billing_address_1 == checkout.billing_address.street_address_1
    assert payment.billing_first_name == checkout.billing_address.first_name
    assert payment.billing_last_name == checkout.billing_address.last_name


def test_checkout_add_payment(
    user_api_client,
    checkout_without_shipping_required,
    address,
    customer_user,
    create_payment_input,
):
    checkout = checkout_without_shipping_required
    checkout.billing_address = address
    checkout.email = "old@example"
    checkout.user = customer_user
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )
    return_url = "https://www.example.com"
    variables = create_payment_input(
        checkout, total.gross.amount, return_url=return_url
    )
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentCreate"]

    assert not data["errors"]
    transactions = data["payment"]["transactions"]
    assert not transactions
    payment = Payment.objects.get()
    assert payment.checkout == checkout
    assert payment.is_active
    assert payment.token == "sample-token"
    assert payment.total == total.gross.amount
    assert payment.currency == total.gross.currency
    assert payment.charge_status == ChargeStatus.NOT_CHARGED
    assert payment.billing_address_1 == checkout.billing_address.street_address_1
    assert payment.billing_first_name == checkout.billing_address.first_name
    assert payment.billing_last_name == checkout.billing_address.last_name
    assert payment.return_url == return_url
    assert payment.billing_email == customer_user.email


@pytest.mark.parametrize("is_amount_fully_covered", (True, False))
def test_checkout_add_payment_checks_if_amount_fully_covered(
    is_amount_fully_covered,
    user_api_client,
    checkout_without_shipping_required,
    address,
    customer_user,
    create_payment_input,
):
    checkout = checkout_without_shipping_required
    checkout.billing_address = address
    checkout.email = "old@example"
    checkout.user = customer_user
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )
    return_url = "https://www.example.com"
    amount = (
        total.gross.amount
        if is_amount_fully_covered
        else total.gross.amount - Decimal("10")
    )
    variables = create_payment_input(
        checkout, amount, return_url=return_url, partial=True
    )
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentCreate"]

    assert not data["errors"]
    payment = Payment.objects.get()
    assert payment.create_order == is_amount_fully_covered


def test_checkout_add_partial_payment(
    user_api_client,
    checkout_without_shipping_required,
    address,
    customer_user,
    create_payment_input,
):
    # given
    checkout = checkout_without_shipping_required
    checkout.billing_address = address
    checkout.email = "old@example"
    checkout.user = customer_user
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )
    quarter_total = total.gross / 4
    assert quarter_total.amount > 0

    partial_payment = Payment.objects.create(
        is_active=True,
        charge_status=ChargeStatus.FULLY_CHARGED,
        total=3 * quarter_total.amount,
        captured_amount=3 * quarter_total.amount,
        checkout=checkout,
        gateway="mirumee.payments.dummy",
    )
    partial_payment.transactions.create(
        kind=TransactionKind.CAPTURE,
        is_success=True,
        amount=3 * quarter_total.amount,
        gateway_response={},
    )

    # when
    return_url = "https://www.example.com"
    variables = create_payment_input(
        checkout, quarter_total.amount, return_url=return_url, partial=True
    )
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentCreate"]

    # then
    assert not data["errors"]
    transactions = data["payment"]["transactions"]
    assert not transactions
    payment = Payment.objects.latest("pk")
    assert payment.checkout == checkout
    assert payment.is_active
    assert payment.token == "sample-token"
    assert payment.total == quarter_total.amount


def test_checkout_add_payment_default_amount(
    user_api_client, checkout_without_shipping_required, address, create_payment_input
):
    checkout = checkout_without_shipping_required
    checkout.billing_address = address
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )

    variables = create_payment_input(checkout, amount=None)
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentCreate"]
    assert not data["errors"]
    transactions = data["payment"]["transactions"]
    assert not transactions
    payment = Payment.objects.get()
    assert payment.checkout == checkout
    assert payment.is_active
    assert payment.token == "sample-token"
    assert payment.total == total.gross.amount
    assert payment.currency == total.gross.currency
    assert payment.charge_status == ChargeStatus.NOT_CHARGED


def test_checkout_add_payment_bad_amount(
    user_api_client, checkout_without_shipping_required, address, create_payment_input
):
    checkout = checkout_without_shipping_required
    checkout.billing_address = address
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )

    variables = create_payment_input(checkout, str(total.gross.amount + Decimal(1)))
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentCreate"]
    assert (
        data["errors"][0]["code"]
        == PaymentErrorCode.PARTIAL_PAYMENT_TOTAL_EXCEEDED.name
    )


def test_checkout_add_payment_bad_partial_amount(
    user_api_client, checkout_without_shipping_required, address, create_payment_input
):
    # given
    checkout = checkout_without_shipping_required
    checkout.billing_address = address
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )
    half_total = total.gross / 2
    assert half_total.amount > 0

    Payment.objects.create(
        is_active=True,
        charge_status=ChargeStatus.FULLY_CHARGED,
        total=half_total.amount,
        captured_amount=half_total.amount,
        checkout=checkout,
    )

    # when
    variables = create_payment_input(
        checkout, str(half_total.amount + Decimal(1)), partial=True
    )
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentCreate"]

    # then
    assert (
        data["errors"][0]["code"]
        == PaymentErrorCode.PARTIAL_PAYMENT_TOTAL_EXCEEDED.name
    )


def test_checkout_add_payment_not_supported_gateways(
    user_api_client, checkout_without_shipping_required, address, create_payment_input
):
    checkout = checkout_without_shipping_required
    checkout.billing_address = address
    checkout.currency = "EUR"
    checkout.save(update_fields=["billing_address", "currency"])

    variables = create_payment_input(checkout, "10.0")
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentCreate"]
    assert data["errors"][0]["code"] == PaymentErrorCode.NOT_SUPPORTED_GATEWAY.name
    assert data["errors"][0]["field"] == "gateway"


def test_use_checkout_billing_address_as_payment_billing(
    user_api_client, checkout_without_shipping_required, address, create_payment_input
):
    checkout = checkout_without_shipping_required
    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )
    variables = create_payment_input(checkout, total.gross.amount)
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentCreate"]

    # check if proper error is returned if address is missing
    assert data["errors"][0]["field"] == "billingAddress"
    assert data["errors"][0]["code"] == PaymentErrorCode.BILLING_ADDRESS_NOT_SET.name

    # assign the address and try again
    address.street_address_1 = "spanish-inqusition"
    address.save()
    checkout.billing_address = address
    checkout.save()
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    get_graphql_content(response)

    checkout.refresh_from_db()
    assert checkout.payments.count() == 1
    payment = checkout.payments.first()
    assert payment.billing_address_1 == address.street_address_1


def test_create_payment_for_checkout_with_active_payments(
    checkout_with_payments, user_api_client, address, create_payment_input
):
    # given
    checkout = checkout_with_payments
    address.street_address_1 = "spanish-inqusition"
    address.save()
    checkout.billing_address = address
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )
    variables = create_payment_input(checkout, total.gross.amount)

    payments_count = checkout.payments.count()
    previous_active_payments_count = checkout.payments.filter(is_active=True).count()
    assert previous_active_payments_count > 0

    # when
    response = user_api_client.post_graphql(CREATE_PAYMENT_MUTATION, variables)
    content = get_graphql_content(response)

    # then
    data = content["data"]["checkoutPaymentCreate"]

    assert not data["errors"]
    checkout.refresh_from_db()
    assert checkout.payments.all().count() == payments_count + 1
    active_payments = checkout.payments.all().filter(is_active=True)
    assert active_payments.count() == previous_active_payments_count


def test_create_partial_payments(
    user_api_client, checkout_without_shipping_required, address, create_payment_input
):
    # given
    checkout = checkout_without_shipping_required
    checkout.billing_address = address
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )

    # when
    response_1 = user_api_client.post_graphql(
        CREATE_PAYMENT_MUTATION,
        create_payment_input(
            checkout, total.gross.amount, partial=True, token=uuid4().hex
        ),
    )
    response_2 = user_api_client.post_graphql(
        CREATE_PAYMENT_MUTATION,
        create_payment_input(
            checkout, total.gross.amount, partial=True, token=uuid4().hex
        ),
    )
    content_1 = get_graphql_content(response_1)
    content_2 = get_graphql_content(response_2)
    data_1 = content_1["data"]["checkoutPaymentCreate"]
    data_2 = content_2["data"]["checkoutPaymentCreate"]

    # then
    assert data_1["payment"]
    assert data_2["payment"]
    assert checkout.payments.filter(is_active=True, partial=True).count() == 2


def test_create_subsequent_partial_payment_with_full_payment(
    user_api_client, checkout_without_shipping_required, address, create_payment_input
):
    # given
    checkout = checkout_without_shipping_required
    checkout.billing_address = address
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )

    # when
    response_1 = user_api_client.post_graphql(
        CREATE_PAYMENT_MUTATION,
        create_payment_input(
            checkout,
            total.gross.amount,
        ),
    )
    response_2 = user_api_client.post_graphql(
        CREATE_PAYMENT_MUTATION,
        create_payment_input(
            checkout, total.gross.amount, token=uuid4().hex, partial=True
        ),
    )
    content_1 = get_graphql_content(response_1)
    content_2 = get_graphql_content(response_2)
    data_1 = content_1["data"]["checkoutPaymentCreate"]
    data_2 = content_2["data"]["checkoutPaymentCreate"]

    # then
    assert data_1["payment"]
    assert data_2["payment"]
    assert checkout.payments.filter(is_active=True).count() == 1


@pytest.mark.parametrize("first_payment_is_partial", [True, False])
def test_create_subsequent_full_payment(
    user_api_client,
    checkout_without_shipping_required,
    address,
    first_payment_is_partial,
    create_payment_input,
):
    # given
    checkout = checkout_without_shipping_required
    checkout.billing_address = address
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )

    # when
    response_1 = user_api_client.post_graphql(
        CREATE_PAYMENT_MUTATION,
        create_payment_input(
            checkout,
            total.gross.amount,
            token=uuid4().hex,
            partial=first_payment_is_partial,
        ),
    )
    response_2 = user_api_client.post_graphql(
        CREATE_PAYMENT_MUTATION,
        create_payment_input(
            checkout, total.gross.amount, token=uuid4().hex, partial=False
        ),
    )
    content_1 = get_graphql_content(response_1)
    content_2 = get_graphql_content(response_2)
    data_1 = content_1["data"]["checkoutPaymentCreate"]
    data_2 = content_2["data"]["checkoutPaymentCreate"]

    # then
    assert data_1["payment"]
    assert data_2["payment"]
    assert checkout.payments.filter(is_active=True).count() == 1


CHECKOUT_PAYMENT_COMPLETE_MUTATION = """
    mutation CheckoutPaymentComplete(
        $token: UUID!,
        $paymentId: ID!,
        $redirectUrl: String
    ) {
        checkoutPaymentComplete(
            token: $token,
            paymentId: $paymentId,
            redirectUrl: $redirectUrl
        ) {
            checkout {
                id,
                token
            },
            errors {
                field,
                message,
                variants,
                code
            }
            confirmationNeeded
            confirmationData
        }
    }
    """


@pytest.mark.integration
def test_checkout_payment_complete(
    site_settings,
    user_api_client,
    checkout_with_gift_card,
    gift_card,
    payment_dummy,
    address,
    shipping_method,
):
    # given
    assert not gift_card.last_used_on

    checkout = checkout_with_gift_card
    checkout.shipping_address = address
    checkout.shipping_method = shipping_method
    checkout.billing_address = address
    checkout.store_value_in_metadata(items={"accepted": "true"})
    checkout.store_value_in_private_metadata(items={"accepted": "false"})
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.calculate_checkout_total_with_gift_cards(
        manager, checkout_info, lines, address
    )
    site_settings.automatically_confirm_all_new_orders = True
    site_settings.save()
    payment = payment_dummy
    payment.is_active = True
    payment.order = None
    payment.total = total.gross.amount
    payment.currency = total.gross.currency
    payment.checkout = checkout
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    assert not payment.transactions.exists()

    orders_count_before = Order.objects.count()
    variables = {
        "token": checkout.token,
        "paymentId": payment_id,
        "redirectUrl": "https://www.example.com",
    }

    # when
    response = user_api_client.post_graphql(
        CHECKOUT_PAYMENT_COMPLETE_MUTATION, variables
    )
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentComplete"]
    assert not data["errors"]

    # then
    checkout_token = data["checkout"]["token"]
    assert checkout_token == str(checkout.token)
    assert Order.objects.count() == orders_count_before

    payment.refresh_from_db()
    assert payment.transactions.count() == 1

    gift_card.refresh_from_db()
    assert gift_card.current_balance == gift_card.initial_balance
    assert not gift_card.last_used_on

    assert Checkout.objects.filter(
        pk=checkout.pk
    ).exists(), "Checkout should be present until completed"


@patch.object(PluginsManager, "process_payment")
def test_checkout_payment_complete_confirmation_needed(
    mocked_process_payment,
    user_api_client,
    checkout_with_payments_factory,
    action_required_gateway_response,
    create_payment_input,
):
    # given
    mocked_process_payment.return_value = action_required_gateway_response

    checkout = checkout_with_payments_factory(charge_status=ChargeStatus.NOT_CHARGED)
    payment = checkout.payments.get()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)

    # when
    variables = {
        "token": checkout.token,
        "paymentId": payment_id,
        "redirectUrl": "https://www.example.com",
    }
    response = user_api_client.post_graphql(
        CHECKOUT_PAYMENT_COMPLETE_MUTATION, variables
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentComplete"]
    assert not data["errors"]
    assert data["confirmationNeeded"] is True
    assert data["confirmationData"]

    checkout.refresh_from_db()
    payment.refresh_from_db()
    assert payment.is_active
    assert payment.to_confirm
    assert payment.charge_status == ChargeStatus.NOT_CHARGED
    mocked_process_payment.assert_called_once()


def test_checkout_payment_complete_confirms_payment(
    user_api_client,
    checkout_with_item,
    payment_txn_to_confirm,
    address,
    shipping_method,
):
    # given
    checkout = checkout_with_item
    checkout.shipping_address = address
    checkout.shipping_method = shipping_method
    checkout.billing_address = address
    checkout.save()

    manager = get_plugins_manager()
    lines = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, [], manager)
    total = calculations.checkout_total(
        manager=manager, checkout_info=checkout_info, lines=lines, address=address
    )
    payment = payment_txn_to_confirm
    payment.is_active = True
    payment.order = None
    payment.total = total.gross.amount
    payment.currency = total.gross.currency
    payment.checkout = checkout
    payment.charge_status = ChargeStatus.NOT_CHARGED
    payment.save()
    txn = payment.transactions.get()
    txn.token = ChargeStatus.AUTHORIZED
    txn.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)

    orders_count = Order.objects.count()

    # when
    variables = {
        "token": checkout.token,
        "paymentId": payment_id,
        "redirectUrl": "https://www.example.com",
    }
    response = user_api_client.post_graphql(
        CHECKOUT_PAYMENT_COMPLETE_MUTATION, variables
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["checkoutPaymentComplete"]

    assert not data["errors"]
    assert not data["confirmationNeeded"]

    new_orders_count = Order.objects.count()
    assert new_orders_count == orders_count

    payment.refresh_from_db()
    assert payment.charge_status == ChargeStatus.AUTHORIZED


CAPTURE_QUERY = """
    mutation PaymentCapture($paymentId: ID!, $amount: PositiveDecimal!) {
        paymentCapture(paymentId: $paymentId, amount: $amount) {
            payment {
                id,
                chargeStatus
            }
            errors {
                field
                message
            }
        }
    }
"""


def test_payment_capture_success(
    staff_api_client, permission_manage_orders, payment_txn_preauth
):
    payment = payment_txn_preauth
    assert payment.charge_status == ChargeStatus.AUTHORIZED
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)

    variables = {"paymentId": payment_id, "amount": str(payment_txn_preauth.total)}
    response = staff_api_client.post_graphql(
        CAPTURE_QUERY, variables, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    data = content["data"]["paymentCapture"]
    assert not data["errors"]
    payment_txn_preauth.refresh_from_db()
    assert payment.charge_status == ChargeStatus.FULLY_CHARGED
    assert payment.transactions.count() == 2
    txn = payment.transactions.last()
    assert txn.kind == TransactionKind.CAPTURE


def test_payment_capture_with_invalid_argument(
    staff_api_client, permission_manage_orders, payment_txn_preauth
):
    payment = payment_txn_preauth
    assert payment.charge_status == ChargeStatus.AUTHORIZED
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)

    variables = {"paymentId": payment_id, "amount": 0}
    response = staff_api_client.post_graphql(
        CAPTURE_QUERY, variables, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    data = content["data"]["paymentCapture"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["message"] == "Amount should be a positive number."


def test_payment_capture_with_payment_non_authorized_yet(
    staff_api_client, permission_manage_orders, payment_dummy
):
    """Ensure capture a payment that is set as authorized is failing with
    the proper error message.
    """
    payment = payment_dummy
    payment.charge_status = ChargeStatus.AUTHORIZED
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)

    variables = {"paymentId": payment_id, "amount": 1}
    response = staff_api_client.post_graphql(
        CAPTURE_QUERY, variables, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    data = content["data"]["paymentCapture"]
    assert data["errors"] == [
        {"field": None, "message": "Cannot find successful auth transaction."}
    ]


def test_payment_capture_gateway_error(
    staff_api_client, permission_manage_orders, payment_txn_preauth, monkeypatch
):
    # given
    payment = payment_txn_preauth

    assert payment.charge_status == ChargeStatus.AUTHORIZED
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    variables = {"paymentId": payment_id, "amount": str(payment_txn_preauth.total)}
    monkeypatch.setattr("saleor.payment.gateways.dummy.dummy_success", lambda: False)

    # when
    response = staff_api_client.post_graphql(
        CAPTURE_QUERY, variables, permissions=[permission_manage_orders]
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["paymentCapture"]
    assert data["errors"] == [{"field": None, "message": "Unable to process capture"}]

    payment_txn_preauth.refresh_from_db()
    assert payment.charge_status == ChargeStatus.AUTHORIZED
    assert payment.transactions.count() == 2
    txn = payment.transactions.last()
    assert txn.kind == TransactionKind.CAPTURE
    assert not txn.is_success


@patch(
    "saleor.payment.gateways.dummy_credit_card.plugin."
    "DummyCreditCardGatewayPlugin.DEFAULT_ACTIVE",
    True,
)
def test_payment_capture_gateway_dummy_credit_card_error(
    staff_api_client, permission_manage_orders, payment_txn_preauth, monkeypatch
):
    # given
    token = TOKEN_EXPIRED
    error = TOKEN_VALIDATION_MAPPING[token]

    payment = payment_txn_preauth
    payment.gateway = "mirumee.payments.dummy_credit_card"
    payment.save()

    transaction = payment.transactions.last()
    transaction.token = token
    transaction.save()

    assert payment.charge_status == ChargeStatus.AUTHORIZED
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    variables = {"paymentId": payment_id, "amount": str(payment_txn_preauth.total)}
    monkeypatch.setattr(
        "saleor.payment.gateways.dummy_credit_card.dummy_success", lambda: False
    )

    # when
    response = staff_api_client.post_graphql(
        CAPTURE_QUERY, variables, permissions=[permission_manage_orders]
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["paymentCapture"]
    assert data["errors"] == [{"field": None, "message": error}]

    payment_txn_preauth.refresh_from_db()
    assert payment.charge_status == ChargeStatus.AUTHORIZED
    assert payment.transactions.count() == 2
    txn = payment.transactions.last()
    assert txn.kind == TransactionKind.CAPTURE
    assert not txn.is_success


REFUND_QUERY = """
    mutation PaymentRefund($paymentId: ID!, $amount: PositiveDecimal!) {
        paymentRefund(paymentId: $paymentId, amount: $amount) {
            payment {
                id,
                chargeStatus
            }
            errors {
                field
                message
            }
        }
    }
"""


def test_payment_refund_success(
    staff_api_client, permission_manage_orders, payment_txn_captured
):
    payment = payment_txn_captured
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)

    variables = {"paymentId": payment_id, "amount": str(payment.total)}
    response = staff_api_client.post_graphql(
        REFUND_QUERY, variables, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    data = content["data"]["paymentRefund"]
    assert not data["errors"]
    payment.refresh_from_db()
    assert payment.charge_status == ChargeStatus.FULLY_REFUNDED
    assert payment.transactions.count() == 2
    txn = payment.transactions.last()
    assert txn.kind == TransactionKind.REFUND


def test_payment_refund_with_invalid_argument(
    staff_api_client, permission_manage_orders, payment_txn_captured
):
    payment = payment_txn_captured
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)

    variables = {"paymentId": payment_id, "amount": 0}
    response = staff_api_client.post_graphql(
        REFUND_QUERY, variables, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    data = content["data"]["paymentRefund"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["message"] == "Amount should be a positive number."


def test_payment_refund_error(
    staff_api_client, permission_manage_orders, payment_txn_captured, monkeypatch
):
    payment = payment_txn_captured
    payment.charge_status = ChargeStatus.FULLY_CHARGED
    payment.captured_amount = payment.total
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    variables = {"paymentId": payment_id, "amount": str(payment.total)}
    monkeypatch.setattr("saleor.payment.gateways.dummy.dummy_success", lambda: False)
    response = staff_api_client.post_graphql(
        REFUND_QUERY, variables, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    data = content["data"]["paymentRefund"]

    assert data["errors"] == [{"field": None, "message": "Unable to process refund"}]
    payment.refresh_from_db()
    assert payment.charge_status == ChargeStatus.FULLY_CHARGED
    assert payment.transactions.count() == 2
    txn = payment.transactions.last()
    assert txn.kind == TransactionKind.REFUND
    assert not txn.is_success


PAYMENT_QUERY = """ query Payments($filter: PaymentFilterInput){
    payments(first: 20, filter: $filter) {
        edges {
            node {
                id
                gateway
                capturedAmount {
                    amount
                    currency
                }
                total {
                    amount
                    currency
                }
                actions
                chargeStatus
                transactions {
                    error
                    gatewayResponse
                    amount {
                        currency
                        amount
                    }
                }
            }
        }
    }
}
"""


def test_payments_query(
    payment_txn_captured, permission_manage_orders, staff_api_client
):
    response = staff_api_client.post_graphql(
        PAYMENT_QUERY, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    data = content["data"]["payments"]["edges"][0]["node"]
    pay = payment_txn_captured
    assert data["gateway"] == pay.gateway
    amount = str(data["capturedAmount"]["amount"])
    assert Decimal(amount) == pay.captured_amount
    assert data["capturedAmount"]["currency"] == pay.currency
    total = str(data["total"]["amount"])
    assert Decimal(total) == pay.total
    assert data["total"]["currency"] == pay.currency
    assert data["chargeStatus"] == PaymentChargeStatusEnum.FULLY_CHARGED.name
    assert data["actions"] == [OrderAction.REFUND.name]
    txn = pay.transactions.get()
    assert data["transactions"] == [
        {
            "amount": {"currency": pay.currency, "amount": float(str(txn.amount))},
            "error": None,
            "gatewayResponse": "{}",
        }
    ]


QUERY_PAYMENT_BY_ID = """
    query payment($id: ID!) {
        payment(id: $id) {
            id,
            pspReference
        }
    }
"""


def test_query_payment(payment_dummy, user_api_client, permission_manage_orders):
    query = QUERY_PAYMENT_BY_ID
    payment = payment_dummy
    payment.psp_reference = "A psp_reference"
    payment.save()
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    variables = {"id": payment_id}
    response = user_api_client.post_graphql(
        query, variables, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    received_id = content["data"]["payment"]["id"]
    assert received_id == payment_id
    psp_reference = content["data"]["payment"]["pspReference"]
    assert psp_reference == "A psp_reference"


def test_staff_query_payment_by_invalid_id(
    staff_api_client, payment_dummy, permission_manage_orders
):
    id = "bh/"
    variables = {"id": id}
    response = staff_api_client.post_graphql(
        QUERY_PAYMENT_BY_ID, variables, permissions=[permission_manage_orders]
    )
    content = get_graphql_content_from_response(response)
    assert len(content["errors"]) == 1
    assert content["errors"][0]["message"] == f"Couldn't resolve id: {id}."
    assert content["data"]["payment"] is None


def test_staff_query_payment_with_invalid_object_type(
    staff_api_client, payment_dummy, permission_manage_orders
):
    variables = {"id": graphene.Node.to_global_id("Order", payment_dummy.pk)}
    response = staff_api_client.post_graphql(
        QUERY_PAYMENT_BY_ID, variables, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    assert content["data"]["payment"] is None


def test_query_payments(payment_dummy, permission_manage_orders, staff_api_client):
    payment = payment_dummy
    payment_id = graphene.Node.to_global_id("Payment", payment.pk)
    response = staff_api_client.post_graphql(
        PAYMENT_QUERY, {}, permissions=[permission_manage_orders]
    )
    content = get_graphql_content(response)
    edges = content["data"]["payments"]["edges"]
    payment_ids = [edge["node"]["id"] for edge in edges]
    assert payment_ids == [payment_id]


def test_query_payments_filter_by_checkout(
    payment_dummy, checkouts_list, permission_manage_orders, staff_api_client
):
    # given
    payment1 = payment_dummy
    payment1.checkout = checkouts_list[0]
    payment1.save()

    payment2 = Payment.objects.get(id=payment1.id)
    payment2.id = None
    payment2.checkout = checkouts_list[1]
    payment2.save()

    payment3 = Payment.objects.get(id=payment1.id)
    payment3.id = None
    payment3.checkout = checkouts_list[2]
    payment3.save()

    variables = {
        "filter": {
            "checkouts": [
                graphene.Node.to_global_id("Checkout", checkout.pk)
                for checkout in checkouts_list[1:4]
            ]
        }
    }

    # when
    response = staff_api_client.post_graphql(
        PAYMENT_QUERY, variables, permissions=[permission_manage_orders]
    )

    # then
    content = get_graphql_content(response)
    edges = content["data"]["payments"]["edges"]
    payment_ids = {edge["node"]["id"] for edge in edges}
    assert payment_ids == {
        graphene.Node.to_global_id("Payment", payment.pk)
        for payment in [payment2, payment3]
    }


def test_query_payments_failed_payment(
    payment_txn_capture_failed, permission_manage_orders, staff_api_client
):
    # given
    payment = payment_txn_capture_failed

    # when
    response = staff_api_client.post_graphql(
        PAYMENT_QUERY, permissions=[permission_manage_orders]
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["payments"]["edges"][0]["node"]

    assert data["gateway"] == payment.gateway
    amount = str(data["capturedAmount"]["amount"])
    assert Decimal(amount) == payment.captured_amount
    assert data["capturedAmount"]["currency"] == payment.currency
    total = str(data["total"]["amount"])
    assert Decimal(total) == payment.total
    assert data["total"]["currency"] == payment.currency
    assert data["chargeStatus"] == PaymentChargeStatusEnum.REFUSED.name
    assert data["actions"] == []
    txn = payment.transactions.get()
    assert data["transactions"] == [
        {
            "amount": {"currency": payment.currency, "amount": float(str(txn.amount))},
            "error": txn.error,
            "gatewayResponse": json.dumps(txn.gateway_response),
        }
    ]


@pytest.fixture
def braintree_customer_id():
    return "1234"


@pytest.fixture
def dummy_customer_id():
    return "4321"


def test_store_payment_gateway_meta(customer_user, braintree_customer_id):
    gateway_name = "braintree"
    meta_key = "BRAINTREE.customer_id"
    META = {meta_key: braintree_customer_id}
    store_customer_id(customer_user, gateway_name, braintree_customer_id)
    assert customer_user.private_metadata == META
    customer_user.refresh_from_db()
    assert fetch_customer_id(customer_user, gateway_name) == braintree_customer_id


@pytest.fixture
def token_config_with_customer(braintree_customer_id):
    return TokenConfig(customer_id=braintree_customer_id)


@pytest.fixture
def set_braintree_customer_id(customer_user, braintree_customer_id):
    gateway_name = "braintree"
    store_customer_id(customer_user, gateway_name, braintree_customer_id)
    return customer_user


@pytest.fixture
def set_dummy_customer_id(customer_user, dummy_customer_id):
    gateway_name = DUMMY_GATEWAY
    store_customer_id(customer_user, gateway_name, dummy_customer_id)
    return customer_user


def test_list_payment_sources(
    mocker, dummy_customer_id, set_dummy_customer_id, user_api_client, channel_USD
):
    gateway = DUMMY_GATEWAY
    query = """
    {
        me {
            storedPaymentSources {
                gateway
                paymentMethodId
                creditCardInfo {
                    lastDigits
                    brand
                    firstDigits
                }
            }
        }
    }
    """
    card = PaymentMethodInfo(
        last_4="5678",
        first_4="1234",
        exp_year=2020,
        exp_month=12,
        name="JohnDoe",
        brand="cardBrand",
    )
    source = CustomerSource(
        id="payment-method-id", gateway=gateway, credit_card_info=card
    )
    mock_get_source_list = mocker.patch(
        "saleor.graphql.account.resolvers.gateway.list_payment_sources",
        return_value=[source],
        autospec=True,
    )
    response = user_api_client.post_graphql(query)

    mock_get_source_list.assert_called_once_with(gateway, dummy_customer_id, ANY, None)
    content = get_graphql_content(response)["data"]["me"]["storedPaymentSources"]
    assert content is not None and len(content) == 1
    assert content[0] == {
        "gateway": gateway,
        "paymentMethodId": "payment-method-id",
        "creditCardInfo": {
            "firstDigits": "1234",
            "lastDigits": "5678",
            "brand": "cardBrand",
        },
    }


def test_stored_payment_sources_restriction(
    mocker, staff_api_client, customer_user, permission_manage_users
):
    # Only owner of storedPaymentSources can fetch it.
    card = PaymentMethodInfo(last_4="5678", exp_year=2020, exp_month=12, name="JohnDoe")
    source = CustomerSource(id="test1", gateway="dummy", credit_card_info=card)
    mocker.patch(
        "saleor.graphql.account.resolvers.gateway.list_payment_sources",
        return_value=[source],
        autospec=True,
    )

    customer_user_id = graphene.Node.to_global_id("User", customer_user.pk)
    query = """
        query PaymentSources($id: ID!) {
            user(id: $id) {
                storedPaymentSources {
                    creditCardInfo {
                        firstDigits
                    }
                }
            }
        }
    """
    variables = {"id": customer_user_id}
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    assert_no_permission(response)


PAYMENT_INITIALIZE_MUTATION = """
mutation PaymentInitialize(
    $gateway: String!,$channel: String!, $paymentData: JSONString){
      paymentInitialize(gateway: $gateway, channel: $channel, paymentData: $paymentData)
      {
        initializedPayment{
          gateway
          name
          data
        }
        errors{
          field
          message
        }
      }
}
"""


@patch.object(PluginsManager, "initialize_payment")
def test_payment_initialize(mocked_initialize_payment, api_client, channel_USD):
    exected_initialize_payment_response = InitializedPaymentResponse(
        gateway="gateway.id",
        name="PaymentPluginName",
        data={
            "epochTimestamp": 1604652056653,
            "expiresAt": 1604655656653,
            "merchantSessionIdentifier": "SSH5EFCB46BA25C4B14B3F37795A7F5B974_BB8E",
        },
    )
    mocked_initialize_payment.return_value = exected_initialize_payment_response

    query = PAYMENT_INITIALIZE_MUTATION
    variables = {
        "gateway": exected_initialize_payment_response.gateway,
        "channel": channel_USD.slug,
        "paymentData": json.dumps(
            {"paymentMethod": "applepay", "validationUrl": "https://127.0.0.1/valid"}
        ),
    }
    response = api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    init_payment_data = content["data"]["paymentInitialize"]["initializedPayment"]
    assert init_payment_data["gateway"] == exected_initialize_payment_response.gateway
    assert init_payment_data["name"] == exected_initialize_payment_response.name
    assert (
        json.loads(init_payment_data["data"])
        == exected_initialize_payment_response.data
    )


def test_payment_initialize_gateway_doesnt_exist(api_client, channel_USD):
    query = PAYMENT_INITIALIZE_MUTATION
    variables = {
        "gateway": "wrong.gateway",
        "channel": channel_USD.slug,
        "paymentData": json.dumps(
            {"paymentMethod": "applepay", "validationUrl": "https://127.0.0.1/valid"}
        ),
    }
    response = api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    assert content["data"]["paymentInitialize"]["initializedPayment"] is None


@patch.object(PluginsManager, "initialize_payment")
def test_payment_initialize_plugin_raises_error(
    mocked_initialize_payment, api_client, channel_USD
):
    error_msg = "Missing paymentMethod field."
    mocked_initialize_payment.side_effect = PaymentError(error_msg)

    query = PAYMENT_INITIALIZE_MUTATION
    variables = {
        "gateway": "gateway.id",
        "channel": channel_USD.slug,
        "paymentData": json.dumps({"validationUrl": "https://127.0.0.1/valid"}),
    }
    response = api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    initialized_payment_data = content["data"]["paymentInitialize"][
        "initializedPayment"
    ]
    errors = content["data"]["paymentInitialize"]["errors"]
    assert initialized_payment_data is None
    assert len(errors) == 1
    assert errors[0]["field"] == "paymentData"
    assert errors[0]["message"] == error_msg
