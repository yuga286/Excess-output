import frappe
from frappe.utils import flt, cint


@frappe.whitelist()
def get_work_order_stock_items(work_order):

	wo = frappe.get_doc("Work Order", work_order)
	finished_item = wo.production_item

	stock_entries = frappe.get_all(
		"Stock Entry",
		filters={
			"work_order": work_order,
			"docstatus": 1
		},
		fields=["name", "creation"],
		order_by="creation asc"
	)

	item_map = {}
	order_counter = 0

	for se in stock_entries:
		rows = frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": se.name},
			fields=[
				"item_code",
				"qty",
				"uom",
				"stock_uom",
				"is_scrap_item"
			],
			order_by="idx asc"
		)

		for r in rows:
			key = r.item_code

			if key not in item_map:
				item_map[key] = {
					"item_code": r.item_code,
					"qty": 0,
					"uom": r.uom,
					"stock_uom": r.stock_uom,
					"is_scrap_item": cint(r.is_scrap_item),
					"is_finished_item": 1 if r.item_code == finished_item else 0,
					"_order": order_counter,
                    "allow_zero_valuation_rate": 1
				}
				order_counter += 1

			item_map[key]["qty"] += flt(r.qty)

	result = sorted(item_map.values(), key=lambda x: x["_order"])
	for r in result:
		r.pop("_order", None)

	return result




# work hard

import frappe
from frappe.utils import flt, nowdate
from frappe import cint
def get_actual_qty(item_code, t_warehouse, s_warehouse):

    wh = s_warehouse or t_warehouse

    if not wh:
        frappe.throw(
            f"Warehouse is mandatory for item {item_code}. "
            f"Source or Target warehouse must be provided."
        )

    if not isinstance(wh, str):
        frappe.throw(
            f"Invalid warehouse value for item {item_code}: {wh}"
        )

    return flt(
        frappe.db.get_value(
            "Bin",
            {
                "item_code": item_code,
                "warehouse": wh
            },
            "actual_qty"
        ) or 0
    )


@frappe.whitelist()
def create_work_order_adjustments(work_order, items, branch, t_warehouse, s_warehouse):
    items = frappe.parse_json(items)
    if not items:
        frappe.throw("No items to adjust")

    wo = frappe.get_doc("Work Order", work_order)

    t_WH = t_warehouse
    SCRAP_WH = s_warehouse
    DEFAULT_BRANCH = branch
    scrap_items = []
    fg_items = []

    # -------------------------
    # CLASSIFY (delta based)
    # -------------------------
    for r in items:
        delta = flt(r.get("qty"))
        if delta == 0:
            continue
        
        actual_qty = 0
        
        item_code = r.get("item_code")
        is_scrap = cint(r.get("is_scrap_item"))
        is_fg = cint(r.get("is_finished_item"))
        
        if is_fg:
            actual_qty = get_actual_qty(item_code, t_WH, SCRAP_WH)
            actual_qty = get_actual_qty(item_code, t_WH, None)
            fg_items.append({
                "item_code": item_code,
                "delta": delta,
                "actual_qty": actual_qty
            })

        elif is_scrap:
            scrap_items.append({
                "item_code": item_code,
                "delta": delta
            })


    created_docs = {}

    try:    
        # ---------------------------------
        # STEP 1: SPLIT SCRAP (+ / -)
        # ---------------------------------
        scrap_receipt_items = [r for r in scrap_items if flt(r["delta"]) > 0]
        scrap_issue_items   = [r for r in scrap_items if flt(r["delta"]) < 0]
        
        # ---------------------------------
        # STEP 2: SCRAP RECEIPT (Material Receipt)
        # ---------------------------------
        if scrap_receipt_items:
            se_scrap_receipt = frappe.new_doc("Stock Entry")
            se_scrap_receipt.company = wo.company
            se_scrap_receipt.branch = DEFAULT_BRANCH
            se_scrap_receipt.custom_work_order_1 = wo.name
            se_scrap_receipt.posting_date = nowdate()
            se_scrap_receipt.stock_entry_type = "Material Receipt"

            for r in scrap_receipt_items:
                se_scrap_receipt.append("items", {
                    "item_code": r["item_code"],
                    "qty": abs(r["delta"]),
                    "t_warehouse": SCRAP_WH,
                    "is_scrap_item": 1,
                    "allow_zero_valuation_rate": 1
                })

            se_scrap_receipt.insert(ignore_permissions=True)
            se_scrap_receipt.submit()
            created_docs["scrap_receipt_se"] = se_scrap_receipt.name
            
        # ---------------------------------
        # STEP 3: SCRAP ISSUE (Scrap Issue)
        # ---------------------------------
        if scrap_issue_items:
            se_scrap_issue = frappe.new_doc("Stock Entry")
            se_scrap_issue.company = wo.company
            se_scrap_issue.branch = DEFAULT_BRANCH
            se_scrap_issue.custom_work_order_1 = wo.name
            se_scrap_issue.posting_date = nowdate()
            se_scrap_issue.stock_entry_type = "Scrap Issue"

            for r in scrap_issue_items:
                se_scrap_issue.append("items", {
                    "item_code": r["item_code"],
                    "qty": abs(r["delta"]),
                    "s_warehouse": SCRAP_WH,
                    "is_scrap_item": 1,
                    "allow_zero_valuation_rate": 1
                })

            se_scrap_issue.insert(ignore_permissions=True)
            se_scrap_issue.submit()
            created_docs["scrap_issue_se"] = se_scrap_issue.name



        # =================================================
        # FG → ONE STOCK ENTRY ONLY
        # =================================================
        if fg_items:
            se_fg = frappe.new_doc("Stock Entry")
            se_fg.company = wo.company
            se_fg.branch = DEFAULT_BRANCH
            se_fg.custom_work_order_1 = wo.name
            se_fg.posting_date = nowdate()

            has_receipt = any(r["delta"] > 0 for r in fg_items)
            has_issue   = any(r["delta"] < 0 for r in fg_items)

            se_fg.stock_entry_type = (
                "Material Receipt" if has_receipt and not has_issue
                else "Material Issue"
            )

            for r in fg_items:
                delta = r["delta"]
                # actual_qty = flt(r.get("actual_qty", 0))   #  FIRST
                # delta = flt(r.get("delta", 0))
                # final_qty = actual_qty + delta

                if delta > 0:
                    se_fg.append("items", {
                        "item_code": r["item_code"],
                        "qty": abs(r["delta"]),
                        # "qty": final_qty,
                        "t_warehouse": t_WH,
                        "s_warehouse": SCRAP_WH,
                        "is_finished_item": 1,
                        "allow_zero_valuation_rate": 1
                    })
                else:
                    se_fg.append("items", {
                        "item_code": r["item_code"],
                        "qty": abs(r["delta"]),
                        "t_warehouse": t_WH,
                        "s_warehouse": SCRAP_WH,
                        "is_finished_item": 1,
                        "allow_zero_valuation_rate": 1
                    })
            

            se_fg.insert(ignore_permissions=True)
            se_fg.submit()
            created_docs["fg_se"] = se_fg.name
                 
            # for item_code, qty in scrap_items:
            for r in scrap_items:
                wo.append("custom_post_production_adjustment", {
                    "adjustment_type": "Excess Scrap",
                    "work_order_no": wo.name,
                    # "item_code": item_code,
                    "item_code":  r["item_code"],
                    "qty": r["delta"],
                    "posting_date": nowdate(),
                    "stock_entry": se_fg.name
                })
                
            wo.flags.ignore_validate_update_after_submit = True
            wo.save(ignore_permissions=True)
            frappe.db.commit()
            
            
            
        # =================================================
        # FG → ONE STOCK RECONCILIATION (ONLY IF REAL CHANGE)
        # =================================================
        if fg_items:      
            sr = frappe.new_doc("Stock Reconciliation")
            sr.company = wo.company
            sr.branch = DEFAULT_BRANCH
            sr.purpose = "Stock Reconciliation"
            sr.posting_date = nowdate()
            # ---- FIX 1: Filter FG items with real change ----
            fg_items_with_change = [
                r for r in fg_items if flt(r.get("delta", 0)) != 0
            ]
            
            # final_qty = flt(r["actual_qty"]) + flt(r["delta"])


            for r in fg_items_with_change:
                actual_qty = flt(
                    frappe.db.get_value(
                        "Bin",
                        {
                            "item_code": r["item_code"],
                            "warehouse": t_WH
                        },
                        "actual_qty"
                    ) or 0
                )
                delta = flt(r.get("delta", 0))
                final_qty = actual_qty + delta

                
                if flt(final_qty, 6) == flt(actual_qty, 6):
                    continue
                
                sr.append("items", {
                    "item_code": r["item_code"],
                    "warehouse": t_WH,
                    "qty": final_qty,
                    "allow_zero_valuation_rate": 1
                })
                
            sr.insert(ignore_permissions=True)
            sr.submit()
            created_docs["fg_reco"] = sr.name
            for r in fg_items:
                if flt(r["delta"]) != 0:
                    wo.append("custom_post_production_adjustment", {
                        "adjustment_type": "Excess FG",
                        "work_order_no": wo.name,
                        "item_code": r["item_code"],
                        "qty": r["delta"],
                        "posting_date": nowdate(),
                        "reconciliation": sr.name
                    })
            
            wo.flags.ignore_validate_update_after_submit = True
            wo.save(ignore_permissions=True)
            frappe.db.commit()        
            
           

    except Exception:
        frappe.db.rollback()
        raise


