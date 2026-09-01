import frappe
from frappe.utils import flt

from erpnext.stock.doctype.inventory_dimension.inventory_dimension import get_inventory_dimensions
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import (
	StockReconciliation,
	get_stock_balance_for,
)


class CustomStockReconciliation(StockReconciliation):
	def validate_inventory_dimension(self):
		"""Allow normal stock reconciliation against inventory dimensions.

		ERPNext upstream only allows dimensioned Stock Reconciliations as opening
		entries. GCF needs Branch/dimension-wise physical stock corrections, so
		we remove only that restriction and keep the rest of ERPNext validation.
		"""
		return

	def get_sle_for_items(self, row, serial_nos=None, current_bundle=True):
		data = super().get_sle_for_items(row, serial_nos=serial_nos, current_bundle=current_bundle)

		if not self._should_use_dimension_reconciliation_delta(row):
			return data

		target_qty = flt(row.qty, row.precision("qty"))
		current_qty = self._get_dimension_current_qty(row)
		qty_delta = flt(target_qty - current_qty, row.precision("qty"))

		data.actual_qty = qty_delta
		data.qty_after_transaction = target_qty if self.docstatus == 1 else current_qty

		if self.docstatus == 1:
			if qty_delta > 0:
				data.incoming_rate = flt(row.valuation_rate)
			elif qty_delta < 0:
				data.outgoing_rate = flt(row.current_valuation_rate or row.valuation_rate)

		return data

	def _should_use_dimension_reconciliation_delta(self, row):
		if self.docstatus != 1:
			return False

		item = frappe.get_cached_value(
			"Item", row.item_code, ["has_serial_no", "has_batch_no"], as_dict=1
		)
		if item.has_serial_no or item.has_batch_no:
			return False

		if row.batch_no or row.serial_no or row.serial_and_batch_bundle or row.current_serial_and_batch_bundle:
			return False

		return self._has_inventory_dimension(row)

	def _get_dimension_current_qty(self, row):
		item_dict = get_stock_balance_for(
			row.item_code,
			row.warehouse,
			self.posting_date,
			self.posting_time,
			batch_no=row.batch_no,
			inventory_dimensions_dict=self._get_inventory_dimensions_dict(row),
			row=row,
			company=self.company,
		)

		row.current_qty = flt(item_dict.get("qty"), row.precision("current_qty"))
		row.current_valuation_rate = flt(
			item_dict.get("rate"), row.precision("current_valuation_rate")
		)

		return row.current_qty

	def _has_inventory_dimension(self, row):
		return bool(self._get_inventory_dimensions_dict(row))

	def _get_inventory_dimensions_dict(self, row):
		dimensions = {}

		for dimension in get_inventory_dimensions():
			fieldname = dimension.get("fieldname")
			source_fieldname = dimension.get("source_fieldname")
			value = row.get(source_fieldname) or row.get(fieldname)

			if not value:
				value = self._get_parent_inventory_dimension_value(source_fieldname, fieldname)

			if value:
				dimensions[fieldname] = value

		return dimensions

	def _get_parent_inventory_dimension_value(self, source_fieldname, fieldname):
		for parent_fieldname in (source_fieldname, fieldname):
			if parent_fieldname and self.meta.has_field(parent_fieldname) and self.get(parent_fieldname):
				return self.get(parent_fieldname)

		return None
