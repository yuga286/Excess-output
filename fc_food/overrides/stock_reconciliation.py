import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext.stock.doctype.inventory_dimension.inventory_dimension import get_inventory_dimensions
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import (
	StockReconciliation,
	get_stock_balance_for,
)
from erpnext.stock.stock_ledger import get_previous_sle
from erpnext.stock.serial_batch_bundle import update_batch_qty

from fc_food.overrides.stock_ledger import make_sl_entries


class CustomStockReconciliation(StockReconciliation):
	def validate_inventory_dimension(self):
		"""Allow normal stock reconciliation against inventory dimensions.

		ERPNext upstream only allows dimensioned Stock Reconciliations as opening
		entries. GCF needs Branch/dimension-wise physical stock corrections, so
		we remove only that restriction and keep the rest of ERPNext validation.
		"""
		return

	def update_stock_ledger(self, allow_negative_stock=False):
		"""Create SLEs using dimension-aware no-change checks for plain items."""
		sl_entries = []
		for row in self.items:
			if not row.qty and not row.valuation_rate and not row.current_qty:
				self.make_adjustment_entry(row, sl_entries)
				continue

			item = frappe.get_cached_value(
				"Item", row.item_code, ["has_serial_no", "has_batch_no"], as_dict=1
			)

			if item.has_serial_no or item.has_batch_no:
				self.get_sle_for_serialized_items(row, sl_entries)
				continue

			if row.serial_and_batch_bundle:
				frappe.throw(
					_(
						"Row #{0}: Item {1} is not a Serialized/Batched Item. It cannot have a Serial No/Batch No against it."
					).format(row.idx, frappe.bold(row.item_code))
				)

			previous_sle = get_previous_sle(
				{
					"item_code": row.item_code,
					"warehouse": row.warehouse,
					"posting_date": self.posting_date,
					"posting_time": self.posting_time,
				}
			)

			if previous_sle:
				if row.qty in ("", None):
					row.qty = previous_sle.get("qty_after_transaction", 0)

				if row.valuation_rate in ("", None):
					row.valuation_rate = previous_sle.get("valuation_rate", 0)

			if row.qty and not row.valuation_rate and not row.allow_zero_valuation_rate:
				frappe.throw(
					_("Valuation Rate required for Item {0} at row {1}").format(row.item_code, row.idx)
				)

			if self._has_inventory_dimension(row):
				if self._dimension_row_has_no_change(row):
					continue
			elif (
				previous_sle
				and row.qty == previous_sle.get("qty_after_transaction")
				and (row.valuation_rate == previous_sle.get("valuation_rate") or row.qty == 0)
			) or (not previous_sle and not row.qty):
				continue

			sl_entries.append(self.get_sle_for_items(row))

		if sl_entries:
			if not allow_negative_stock:
				allow_negative_stock = cint(
					frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
				)

			self.make_sl_entries(sl_entries, allow_negative_stock=allow_negative_stock)
		elif self.docstatus == 1:
			frappe.throw(
				_(
					"No stock ledger entries were created. Please set the quantity or valuation rate for the items properly and try again."
				)
			)

	def get_sle_for_items(self, row, serial_nos=None, current_bundle=True):
		data = super().get_sle_for_items(row, serial_nos=serial_nos, current_bundle=current_bundle)

		if not self._should_use_dimension_reconciliation_delta(row, data):
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
		else:
			data.previous_qty_after_transaction = target_qty

		return data

	def make_sl_entries(self, sl_entries, allow_negative_stock=False, via_landed_cost_voucher=False):
		make_sl_entries(sl_entries, allow_negative_stock, via_landed_cost_voucher)
		update_batch_qty(
			self.doctype, self.name, self.docstatus, via_landed_cost_voucher=via_landed_cost_voucher
		)

	def _should_use_dimension_reconciliation_delta(self, row, data):
		if self.docstatus not in (1, 2):
			return False

		item = frappe.get_cached_value(
			"Item", row.item_code, ["has_serial_no", "has_batch_no"], as_dict=1
		)
		if item.has_serial_no or item.has_batch_no:
			return False

		if row.batch_no or row.serial_no or row.serial_and_batch_bundle or row.current_serial_and_batch_bundle:
			return False

		return self._has_inventory_dimension(row, data)

	def _dimension_row_has_no_change(self, row):
		current_qty = self._get_dimension_current_qty(row)
		current_rate = flt(row.current_valuation_rate, row.precision("current_valuation_rate"))
		target_rate = flt(row.valuation_rate, row.precision("valuation_rate"))

		return flt(row.qty, row.precision("qty")) == current_qty and (
			target_rate == current_rate or not row.qty
		)

	def _get_dimension_current_qty(self, row):
		if self.docstatus != 1:
			return flt(row.current_qty, row.precision("current_qty"))

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

	def _has_inventory_dimension(self, row, data=None):
		return bool(self._get_inventory_dimensions_dict(row, data=data))

	def _get_inventory_dimensions_dict(self, row, data=None):
		dimensions = {}

		for dimension in get_inventory_dimensions():
			fieldname = dimension.get("fieldname")
			source_fieldname = dimension.get("source_fieldname")
			value = row.get(source_fieldname) or row.get(fieldname)

			if data:
				value = value or data.get(fieldname)

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
