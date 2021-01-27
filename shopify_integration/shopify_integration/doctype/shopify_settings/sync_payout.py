from collections import defaultdict

from shopify import Order, PaginatedIterator, Payouts, Transactions

import frappe
from frappe.utils import flt, getdate, now

from shopify_integration.connector import (
	create_shopify_delivery, create_shopify_invoice, create_shopify_order,
	get_shopify_document, get_tax_account_head)
from shopify_integration.shopify_integration.doctype.shopify_log.shopify_log import make_shopify_log


@frappe.whitelist()
def sync_payouts_from_shopify():
	"""
	Pull and sync payouts from Shopify Payments transactions with existing orders
	"""

	if not frappe.db.get_single_value("Shopify Settings", "enable_shopify"):
		return

	frappe.enqueue(method=create_shopify_payouts, queue='long', is_async=True)
	return True


def get_payouts():
	shopify_settings = frappe.get_single("Shopify Settings")

	kwargs = dict()
	if shopify_settings.last_sync_datetime:
		kwargs['date_min'] = shopify_settings.last_sync_datetime

	session = shopify_settings.get_shopify_session()
	Payouts.activate_session(session)

	try:
		payouts = PaginatedIterator(Payouts.find(**kwargs))
	except Exception as e:
		make_shopify_log(status="Payout Error", exception=e, rollback=True)
		return []
	else:
		return payouts
	finally:
		Payouts.clear_session()


def create_shopify_payouts():
	payouts = get_payouts()
	if not payouts:
		return

	shopify_settings = frappe.get_single("Shopify Settings")
	session = shopify_settings.get_shopify_session()

	for page in payouts:
		for payout in page:
			if frappe.db.exists("Shopify Payout", {"payout_id": payout.id}):
				continue

			payout_order_ids = []
			try:
				Transactions.activate_session(session)
				payout_transactions = Transactions.find(payout_id=payout.id)
			except Exception as e:
				make_shopify_log(status="Payout Transactions Error", response_data=payout.to_dict(), exception=e)
			else:
				payout_order_ids = [transaction.source_order_id for transaction in payout_transactions
					if transaction.source_order_id]
			finally:
				Transactions.clear_session()

			create_missing_orders(session, payout_order_ids)
			payout_doc = create_or_update_shopify_payout(session, payout)
			update_invoice_fees(payout_doc)

	shopify_settings.last_sync_datetime = now()
	shopify_settings.save()


def create_missing_orders(session, shopify_order_ids):
	for shopify_order_id in shopify_order_ids:
		sales_order = get_shopify_document("Sales Order", shopify_order_id)
		sales_invoice = get_shopify_document("Sales Invoice", shopify_order_id)
		delivery_note = get_shopify_document("Delivery Note", shopify_order_id)

		if all([sales_order, sales_invoice, delivery_note]):
			continue

		Order.activate_session(session)
		order = Order.find(shopify_order_id)
		Order.clear_session()

		if not order:
			continue

		# create an order, invoice and delivery, if missing
		if not sales_order:
			sales_order = create_shopify_order(order.to_dict())

		if sales_order:
			if not sales_invoice:
				create_shopify_invoice(order.to_dict(), sales_order)
			if not delivery_note:
				create_shopify_delivery(order.to_dict(), sales_order)


def update_invoice_fees(payout_doc):
	payouts_by_invoice = defaultdict(list)
	for transaction in payout_doc.transactions:
		if transaction.sales_invoice:
			payouts_by_invoice[transaction.sales_invoice].append(transaction)

	for invoice_id, order_transactions in payouts_by_invoice.items():
		invoice = frappe.get_doc("Sales Invoice", invoice_id)
		if invoice.docstatus != 0:
			continue

		for transaction in order_transactions:
			if not transaction.fee:
				continue

			invoice.append("taxes", {
				"charge_type": "Actual",
				"account_head": get_tax_account_head("fee"),
				"description": transaction.transaction_type,
				"tax_amount": -flt(transaction.fee)
			})

		invoice.save()
		invoice.submit()


def create_or_update_shopify_payout(session, payout):
	"""
	Create a Payout document from Shopify's Payout information.
	If a payout exists, update that instead.

	Args:
		session (shopify.Session): The active Shopify client session.
		payout (shopify.Payout): The Payout payload from Shopify

	Returns:
		ShopifyPayout: The created Shopify Payout document
	"""

	company = frappe.db.get_single_value("Shopify Settings", "company")

	payout_doc = frappe.new_doc("Shopify Payout")
	payout_doc.update({
		"company": company,
		"payout_id": payout.id,
		"payout_date": getdate(payout.date),
		"status": frappe.unscrub(payout.status),
		"amount": flt(payout.amount),
		"currency": payout.currency,
		**payout.summary.to_dict()  # unpack the payout amounts and fees from the summary
	})

	try:
		Transactions.activate_session(session)
		payout_transactions = Transactions.find(payout_id=payout.id)
	except Exception as e:
		payout_doc.save()
		make_shopify_log(status="Payout Transactions Error", response_data=payout.to_dict(), exception=e)
		return payout_doc.name
	finally:
		Transactions.clear_session()

	payout_doc.set("transactions", [])
	for transaction in payout_transactions:
		shopify_order_id = transaction.source_order_id

		order_financial_status = None
		if shopify_order_id:
			Order.activate_session(session)
			order = Order.find(shopify_order_id)
			Order.clear_session()
			order_financial_status = frappe.unscrub(order.financial_status)

		total_amount = -flt(transaction.amount) if transaction.type == "payout" else flt(transaction.amount)
		net_amount = -flt(transaction.net) if transaction.type == "payout" else flt(transaction.net)

		sales_order = get_shopify_document("Sales Order", shopify_order_id)
		sales_invoice = get_shopify_document("Sales Invoice", shopify_order_id)
		delivery_note = get_shopify_document("Delivery Note", shopify_order_id)

		payout_doc.append("transactions", {
			"transaction_id": transaction.id,
			"transaction_type": frappe.unscrub(transaction.type),
			"processed_at": getdate(transaction.processed_at),
			"total_amount": total_amount,
			"fee": flt(transaction.fee),
			"net_amount": net_amount,
			"currency": transaction.currency,
			"sales_order": sales_order.name if sales_order else None,
			"sales_invoice": sales_invoice.name if sales_invoice else None,
			"delivery_note": delivery_note.name if delivery_note else None,
			"source_id": transaction.source_id,
			"source_type": frappe.unscrub(transaction.source_type),
			"source_order_financial_status": order_financial_status,
			"source_order_id": shopify_order_id,
			"source_order_transaction_id": transaction.source_order_transaction_id,
		})

	payout_doc.save()
	frappe.db.commit()
	return payout_doc