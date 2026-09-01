import frappe
from frappe.query_builder import Order
from frappe.utils import cint, flt, now, nowdate

from erpnext.controllers.stock_controller import future_sle_exists, invalidate_future_sle_cache
from erpnext.stock.doctype.bin.bin import update_qty as update_bin_qty
from erpnext.stock.doctype.inventory_dimension.inventory_dimension import get_inventory_dimensions
from erpnext.stock.stock_ledger import (
	NegativeStockError,
	get_args_for_future_sle,
	get_datetime_limit_condition,
	get_incoming_outgoing_rate_for_cancel,
	get_or_make_bin,
	get_combine_datetime,
	is_negative_with_precision,
	is_negative_stock_allowed,
	make_entry,
	set_as_cancel,
	update_entries_after,
	validate_cancellation,
	validate_stock_frozen_by_closing_entry,
)


def make_sl_entries(sl_entries, allow_negative_stock=False, via_landed_cost_voucher=False):
	if not sl_entries:
		return

	validate_stock_frozen_by_closing_entry(sl_entries)

	cancelled = sl_entries[0].get("is_cancelled")
	if cancelled:
		validate_cancellation(sl_entries)
		set_as_cancel(sl_entries[0].get("voucher_type"), sl_entries[0].get("voucher_no"))

	args = get_args_for_future_sle(sl_entries[0])
	future_sle_exists(args, sl_entries)

	for sle in sl_entries:
		if cancelled:
			sle["actual_qty"] = -flt(sle.get("actual_qty"))

			if sle["actual_qty"] < 0 and not sle.get("outgoing_rate"):
				sle["outgoing_rate"] = get_incoming_outgoing_rate_for_cancel(
					sle.item_code, sle.voucher_type, sle.voucher_no, sle.voucher_detail_no
				)
				sle["incoming_rate"] = 0.0

			if sle["actual_qty"] > 0 and not sle.get("incoming_rate"):
				sle["incoming_rate"] = get_incoming_outgoing_rate_for_cancel(
					sle.item_code, sle.voucher_type, sle.voucher_no, sle.voucher_detail_no
				)
				sle["outgoing_rate"] = 0.0

		if sle.get("actual_qty") or sle.get("voucher_type") == "Stock Reconciliation":
			sle_doc = make_entry(sle, allow_negative_stock, via_landed_cost_voucher)

		args = sle_doc.as_dict()
		args["posting_datetime"] = get_combine_datetime(args.posting_date, args.posting_time)

		if sle.get("voucher_type") == "Stock Reconciliation":
			args.previous_qty_after_transaction = sle.get("previous_qty_after_transaction")

		is_stock_item = frappe.get_cached_value("Item", args.get("item_code"), "is_stock_item")
		if is_stock_item:
			bin_name = get_or_make_bin(args.get("item_code"), args.get("warehouse"))
			args.reserved_stock = flt(frappe.db.get_value("Bin", bin_name, "reserved_stock"))
			repost_current_voucher(
				args, allow_negative_stock, via_landed_cost_voucher, cancelled=cancelled
			)
			update_bin_qty(bin_name, args)
		else:
			frappe.msgprint(f"Item {args.get('item_code')} ignored since it is not a stock item")

	invalidate_future_sle_cache(sl_entries[0].get("voucher_type"), sl_entries[0].get("voucher_no"))


def repost_current_voucher(args, allow_negative_stock=False, via_landed_cost_voucher=False, cancelled=False):
	if not (args.get("actual_qty") or args.get("voucher_type") == "Stock Reconciliation"):
		return

	if not args.get("posting_date"):
		args["posting_date"] = nowdate()

	if not (args.get("is_cancelled") and via_landed_cost_voucher):
		repost_args = {
			"item_code": args.get("item_code"),
			"warehouse": args.get("warehouse"),
			"posting_date": args.get("posting_date"),
			"posting_time": args.get("posting_time"),
			"voucher_type": args.get("voucher_type"),
			"voucher_no": args.get("voucher_no"),
			"sle_id": args.get("name"),
			"creation": args.get("creation"),
			"reserved_stock": args.get("reserved_stock"),
			"cancelled": cancelled,
		}
		repost_args.update(get_inventory_dimension_filters(args))

		DimensionAwareUpdateEntriesAfter(
			repost_args,
			allow_negative_stock=allow_negative_stock,
			via_landed_cost_voucher=via_landed_cost_voucher,
		)

	update_qty_in_future_sle(args, allow_negative_stock)


class DimensionAwareUpdateEntriesAfter(update_entries_after):
	def process_sle_against_current_timestamp(self):
		sl_entries = get_sle_against_current_voucher(self.args)
		if self.args.get("cancelled") and sl_entries:
			self.seed_previous_sle_for_cancellation(sl_entries[0])
		for sle in sl_entries:
			sle["timestamp"] = sle.posting_datetime
			self.process_sle(sle)

	def seed_previous_sle_for_cancellation(self, anchor_sle):
		key = (anchor_sle.item_code, anchor_sle.warehouse)
		if key in self.prev_sle_dict:
			return

		args = frappe._dict(anchor_sle)
		args["sle_id"] = args.name
		prev_sle = get_previous_sle_of_current_voucher(args)
		if prev_sle:
			self.prev_sle_dict[key] = prev_sle

	def initialize_previous_data(self, args):
		self.data.setdefault(args.warehouse, frappe._dict())
		warehouse_dict = self.data[args.warehouse]

		if self.stock_ledgers_to_repost:
			return

		previous_sle = get_previous_sle_of_current_voucher(args)
		if previous_sle:
			self.prev_sle_dict[(args.get("item_code"), args.get("warehouse"))] = previous_sle

		warehouse_dict.previous_sle = previous_sle

		for key in ("qty_after_transaction", "valuation_rate", "stock_value"):
			setattr(warehouse_dict, key, flt(previous_sle.get(key)))

		warehouse_dict.update(
			{
				"prev_stock_value": previous_sle.stock_value or 0.0,
				"stock_queue": frappe.parse_json(previous_sle.stock_queue or "[]"),
				"stock_value_difference": 0.0,
			}
		)

	def get_future_entries_to_repost(self, kwargs):
		return get_stock_ledger_entries(kwargs, ">=", "asc", for_update=True, check_serial_no=False)

	def process_sle(self, sle):
		key = (sle.item_code, sle.warehouse)
		if key not in self.prev_sle_dict:
			self.prev_sle_dict[key] = get_previous_sle_of_current_voucher(sle)

		super().process_sle(sle)


def get_inventory_dimension_filters(source):
	filters = {}
	for dimension in get_inventory_dimensions():
		fieldname = dimension.get("fieldname")
		if fieldname:
			filters[fieldname] = source.get(fieldname)

	return filters


def get_inventory_dimension_conditions(args):
	conditions = []
	values = {}

	for dimension in get_inventory_dimensions():
		fieldname = dimension.get("fieldname")
		if not fieldname:
			continue

		value_key = f"dimension_{fieldname}"
		values[value_key] = args.get(fieldname)
		column = f"`{fieldname.replace('`', '')}`"

		if args.get(fieldname):
			conditions.append(f"{column} = %({value_key})s")
		else:
			conditions.append(f"({column} is null or {column} = '')")

	return conditions, values


def get_stock_ledger_entries(
	previous_sle,
	operator=None,
	order="desc",
	limit=None,
	for_update=False,
	debug=False,
	check_serial_no=True,
	extra_cond=None,
	for_report=False,
):
	dimension_conditions, dimension_values = get_inventory_dimension_conditions(previous_sle)
	extra_conditions = ""
	if dimension_conditions:
		extra_conditions = " and " + " and ".join(dimension_conditions)

	return frappe.get_attr("erpnext.stock.stock_ledger.get_stock_ledger_entries")(
		_get_args_with_dimensions(previous_sle, dimension_values),
		operator=operator,
		order=order,
		limit=limit,
		for_update=for_update,
		debug=debug,
		check_serial_no=check_serial_no,
		extra_cond=(extra_cond or "") + extra_conditions,
		for_report=for_report,
	)


def _get_args_with_dimensions(args, dimension_values):
	query_args = frappe._dict(args)
	query_args.update(dimension_values)
	return query_args


def get_sle_against_current_voucher(kwargs):
	kwargs["posting_datetime"] = get_combine_datetime(kwargs.posting_date, kwargs.posting_time)
	doctype = frappe.qb.DocType("Stock Ledger Entry")

	query = (
		frappe.qb.from_(doctype)
		.select("*")
		.where(
			(doctype.item_code == kwargs.item_code)
			& (doctype.warehouse == kwargs.warehouse)
			& (doctype.is_cancelled == 0)
			& (doctype.posting_datetime == kwargs.posting_datetime)
		)
		.orderby(doctype.creation, order=Order.asc)
		.for_update()
	)

	for dimension in get_inventory_dimensions():
		fieldname = dimension.get("fieldname")
		if not fieldname:
			continue

		if kwargs.get(fieldname):
			query = query.where(doctype[fieldname] == kwargs.get(fieldname))
		else:
			query = query.where((doctype[fieldname].isnull()) | (doctype[fieldname] == ""))

	if not kwargs.get("cancelled"):
		query = query.where(doctype.creation == kwargs.creation)

	return query.run(as_dict=True)


def get_previous_sle_of_current_voucher(args, operator="<", exclude_current_voucher=False):
	if not args.get("posting_date"):
		args["posting_datetime"] = "1900-01-01 00:00:00"

	if not args.get("posting_datetime"):
		args["posting_datetime"] = get_combine_datetime(args["posting_date"], args["posting_time"])

	voucher_condition = ""
	if exclude_current_voucher:
		voucher_no = args.get("voucher_no")
		voucher_condition = f"and voucher_no != {frappe.db.escape(voucher_no)}"
	elif args.get("creation") and args.get("sle_id") and not args.get("cancelled"):
		creation = args.get("creation")
		operator = "<="
		voucher_condition = f"and creation < {frappe.db.escape(creation)}"

	dimension_conditions, dimension_values = get_inventory_dimension_conditions(args)
	if dimension_conditions:
		voucher_condition += " and " + " and ".join(dimension_conditions)

	sql_args = {
		"item_code": args.get("item_code"),
		"warehouse": args.get("warehouse"),
		"posting_datetime": args.get("posting_datetime"),
	}
	sql_args.update(dimension_values)

	sle = frappe.db.sql(
		f"""
		select *, posting_datetime as "timestamp"
		from `tabStock Ledger Entry`
		where item_code = %(item_code)s
			and warehouse = %(warehouse)s
			and is_cancelled = 0
			{voucher_condition}
			and posting_datetime {operator} %(posting_datetime)s
		order by posting_datetime desc, creation desc
		limit 1
		for update""",
		sql_args,
		as_dict=1,
	)

	return sle[0] if sle else frappe._dict()


def update_qty_in_future_sle(args, allow_negative_stock=False):
	qty_shift = args.actual_qty
	posting_datetime = get_combine_datetime(args["posting_date"], args["posting_time"])
	args["posting_datetime"] = posting_datetime

	if args.voucher_type == "Stock Reconciliation":
		qty_shift = get_stock_reco_qty_shift(args)

	sle = frappe.qb.DocType("Stock Ledger Entry")

	future_condition = sle.posting_datetime > posting_datetime
	if args.get("creation") and not args.get("is_cancelled"):
		future_condition = future_condition | (
			(sle.posting_datetime == posting_datetime) & (sle.creation > args.get("creation"))
		)

	query = (
		frappe.qb.update(sle)
		.set(sle.qty_after_transaction, sle.qty_after_transaction + qty_shift)
		.where(
			(sle.item_code == args.get("item_code"))
			& (sle.warehouse == args.get("warehouse"))
			& (sle.is_cancelled == 0)
			& future_condition
		)
	)

	query = apply_inventory_dimension_query_filters(query, sle, args)

	next_stock_reco_detail = get_next_stock_reco(args)
	if next_stock_reco_detail:
		query = query.where(get_datetime_limit_condition(sle, next_stock_reco_detail[0]))

	query.run()

	validate_negative_qty_in_future_sle(args, allow_negative_stock)


def get_stock_reco_qty_shift(args):
	if args.get("is_cancelled"):
		if args.get("previous_qty_after_transaction"):
			if args.get("serial_and_batch_bundle"):
				return args.get("previous_qty_after_transaction")

			last_balance = args.get("previous_qty_after_transaction")
			return flt(args.qty_after_transaction) - flt(last_balance)

		return flt(args.actual_qty)

	if args.get("serial_and_batch_bundle"):
		return flt(args.actual_qty)

	last_balance = get_previous_sle_of_current_voucher(
		args, "<=", exclude_current_voucher=True
	).get("qty_after_transaction")

	if last_balance is not None:
		return flt(args.qty_after_transaction) - flt(last_balance)

	return args.qty_after_transaction


def get_next_stock_reco(kwargs):
	sle = frappe.qb.DocType("Stock Ledger Entry")

	query = (
		frappe.qb.from_(sle)
		.select(
			sle.name,
			sle.posting_date,
			sle.posting_time,
			sle.creation,
			sle.voucher_no,
			sle.item_code,
			sle.batch_no,
			sle.serial_and_batch_bundle,
			sle.actual_qty,
			sle.has_batch_no,
		)
		.where(
			(sle.item_code == kwargs.get("item_code"))
			& (sle.warehouse == kwargs.get("warehouse"))
			& (sle.voucher_type == "Stock Reconciliation")
			& (sle.voucher_no != kwargs.get("voucher_no"))
			& (sle.is_cancelled == 0)
			& frappe.get_attr("erpnext.stock.stock_ledger.get_next_reco_datetime_condition")(
				sle, kwargs
			)
		)
		.orderby(sle.posting_datetime)
		.orderby(sle.creation)
		.limit(1)
	)

	if kwargs.get("batch_no"):
		query = query.where(sle.batch_no == kwargs.get("batch_no"))

	query = apply_inventory_dimension_query_filters(query, sle, kwargs)

	return query.run(as_dict=True)


def apply_inventory_dimension_query_filters(query, sle, args):
	for dimension in get_inventory_dimensions():
		fieldname = dimension.get("fieldname")
		if not fieldname:
			continue

		if args.get(fieldname):
			query = query.where(sle[fieldname] == args.get(fieldname))
		else:
			query = query.where((sle[fieldname].isnull()) | (sle[fieldname] == ""))

	return query


def validate_negative_qty_in_future_sle(args, allow_negative_stock=False):
	if allow_negative_stock or is_negative_stock_allowed(item_code=args.item_code):
		return

	if args.actual_qty >= 0 and args.voucher_type != "Stock Reconciliation":
		return

	neg_sle = get_future_sle_with_negative_qty(args)
	if is_negative_with_precision(neg_sle):
		frappe.throw(
			f"{abs(neg_sle[0]['qty_after_transaction'])} units of {args.item_code} needed in {args.warehouse} on {neg_sle[0]['posting_date']} {neg_sle[0]['posting_time']} for {neg_sle[0]['voucher_type']} {neg_sle[0]['voucher_no']} to complete this transaction.",
			NegativeStockError,
			title="Insufficient Stock",
		)


def get_future_sle_with_negative_qty(sle_args):
	dimension_conditions, dimension_values = get_inventory_dimension_conditions(sle_args)
	dimension_condition = ""
	if dimension_conditions:
		dimension_condition = " and " + " and ".join(dimension_conditions)

	sql_args = dict(sle_args)
	sql_args.update(dimension_values)

	return frappe.db.sql(
		f"""
		select
			qty_after_transaction, posting_date, posting_time,
			voucher_type, voucher_no
		from `tabStock Ledger Entry`
		where
			item_code = %(item_code)s
			and warehouse = %(warehouse)s
			and voucher_no != %(voucher_no)s
			and posting_datetime >= %(posting_datetime)s
			and is_cancelled = 0
			and qty_after_transaction < 0
			{dimension_condition}
		order by posting_datetime asc, creation asc
		limit 1
		""",
		sql_args,
		as_dict=1,
	)
