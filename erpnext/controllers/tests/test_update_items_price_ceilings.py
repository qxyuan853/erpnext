# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Update Items on a submitted order must respect the same price ceilings as submit.

``validate()`` does not run on an ``update_after_submit`` save, so every ceiling
enforced from ``SellingController.validate()`` / ``SalesOrder.on_submit()`` has to be
re-applied inside update_child_qty_rate. Without that:

* ``Item.max_discount`` is never re-checked;
* ``validate_approving_authority()`` is called without ``doc_obj``, which makes the
  "Average Discount", "Customerwise Discount", "Itemwise Discount" and
  "Item Group wise Discount" rule types silent no-ops;
* the recorded ``discount_percentage`` is forced to 0, hiding the discount from the
  ceilings themselves and from every discount report;
* the ``base_grand_total >= 0`` guard is skipped, so a submitted order can be driven
  to a negative total while ``allow_negative_rates_for_items`` is off.
"""

import json

import frappe

from erpnext.controllers.accounts_controller import update_child_qty_rate
from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order
from erpnext.stock.doctype.item.test_item import make_item
from erpnext.tests.utils import ERPNextTestSuite

CATALOGUE_RATE = 10000
CHEAP_RATE = 100


# Note on scope: these tests assert the refusal only. update_child_qty_rate saves the
# child row before the parent-level ceilings are evaluated, so in a real request the
# throw rolls the whole transaction back and the submitted order keeps its old rate;
# a unit test has no request-level rollback, so the row state after the throw is not
# asserted here.


def _trans_item(so, rate, qty=1):
	return json.dumps(
		[
			{
				"item_code": so.items[0].item_code,
				"qty": qty,
				"rate": rate,
				"docname": so.items[0].name,
			}
		]
	)


class TestUpdateItemsPriceCeilings(ERPNextTestSuite):
	def _order(self, item_code):
		so = make_sales_order(item_code=item_code, qty=1, rate=CATALOGUE_RATE)
		so.reload()
		# make_sales_order() does not fetch an Item Price, so make the catalogue
		# price explicit on the row the ceiling is measured against.
		frappe.db.set_value("Sales Order Item", so.items[0].name, "price_list_rate", CATALOGUE_RATE)
		so.reload()
		return so

	def test_item_max_discount_is_reapplied(self):
		item = make_item(properties={"is_stock_item": 0, "max_discount": 10}).name
		so = self._order(item)
		self.assertRaises(
			frappe.ValidationError,
			update_child_qty_rate,
			"Sales Order",
			_trans_item(so, CHEAP_RATE),
			so.name,
		)

	def test_recorded_discount_percentage_is_truthful(self):
		item = make_item(properties={"is_stock_item": 0}).name
		so = self._order(item)
		update_child_qty_rate("Sales Order", _trans_item(so, 9000), so.name)
		so.reload()
		# 10 000 -> 9 000 is a 10% discount and must be recorded as one.
		self.assertEqual(so.items[0].rate, 9000)
		self.assertEqual(so.items[0].discount_percentage, 10)

	def test_discount_authorization_rule_is_reapplied(self):
		item = make_item(properties={"is_stock_item": 0}).name
		if not frappe.db.exists("Role", "_Test Discount Approver"):
			frappe.get_doc({"doctype": "Role", "role_name": "_Test Discount Approver"}).insert()
		user = "_test_update_items_rep@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user,
					"first_name": "Update Items Rep",
					"send_welcome_email": 0,
					"roles": [{"role": "Sales User"}],
				}
			).insert(ignore_permissions=True)
		rule = frappe.get_doc(
			{
				"doctype": "Authorization Rule",
				"transaction": "Sales Order",
				"based_on": "Average Discount",
				"company": "_Test Company",
				"value": 5,
				"system_role": "Sales User",
				"approving_role": "_Test Discount Approver",
			}
		).insert()
		self.addCleanup(rule.delete)

		so = self._order(item)
		# Administrator implicitly holds every role, so the not-authorized branch
		# would never fire as Administrator.
		with self.set_user(user):
			self.assertRaises(
				frappe.ValidationError,
				update_child_qty_rate,
				"Sales Order",
				_trans_item(so, CHEAP_RATE),
				so.name,
			)

	def test_negative_grand_total_is_rejected(self):
		item = make_item(properties={"is_stock_item": 0}).name
		so = self._order(item)
		frappe.db.set_single_value("Selling Settings", "allow_negative_rates_for_items", 0)
		self.assertRaises(
			frappe.ValidationError,
			update_child_qty_rate,
			"Sales Order",
			_trans_item(so, -CATALOGUE_RATE),
			so.name,
		)
