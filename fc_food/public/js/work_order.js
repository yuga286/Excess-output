frappe.ui.form.on("Work Order", {
    refresh(frm) {
        if (frm.doc.docstatus === 1) {
            frm.add_custom_button("Adjust Items", () => {
                frappe.call({
                    method: "dt_gcfoods_customization.api.get_work_order_stock_items",
                    args: { work_order: frm.doc.name },
                    callback(r) {
                        if (!r.message || !r.message.length) {
                            frappe.msgprint("No Stock Entries found");
                            return;
                        }
                        open_items_popup(frm, r.message);
                    }
                });
            });
        }
    }
});

function open_items_popup(frm, items) {
    let d = new frappe.ui.Dialog({
        title: "Work Order Items Adjustment",
        size: "extra-large",
        fields: [
            {
                fieldname: "branch",
                fieldtype: "Link",
                label: "Branch",
                options: "Branch",
                reqd: 1,
                default: frm.doc.branch || "",
                onchange() {
                    let branch = d.get_value("branch");
                    // clear warehouses when branch changes
                    d.set_value("t_warehouse", null);
                    d.set_value("s_warehouse", null);

                    // Target WH filter
                    d.fields_dict.t_warehouse.get_query = function () {
                        return {
                            filters: {
                                custom_branch: branch
                            }
                        };
                    };

                    // Source WH filter
                    d.fields_dict.s_warehouse.get_query = function () {
                        return {
                            filters: {
                                custom_branch: branch
                            }
                        };
                    };
                }
            },
            {
                fieldtype: "Column Break"
            },
            {
                fieldname: "t_warehouse",
                fieldtype: "Link",
                label: "Target Warehouse",
                options: "Warehouse",
                reqd: 1,
                default: frm.doc.warehouse
            },
            {
                fieldtype: "Column Break"
            },
            {
                fieldname: "s_warehouse",
                fieldtype: "Link",
                label: "Source Warehouse",
                options: "Warehouse",
                reqd: 1,
                default: frm.doc.warehouse
            },
            {
                fieldtype: "Section Break"
            },
            {
            fieldname: "items",
            fieldtype: "Table",
            cannot_add_rows: 1,
            in_place_edit: true,
            fields: [
                { fieldname: "item_code", fieldtype: "Link", options: "Item", read_only: 1, in_list_view: 1 },
                { fieldname: "qty", fieldtype: "Float", label: "Actual Qty", in_list_view: 1 },
                { fieldname: "delta_qty", fieldtype: "Float", label: "Delta Qty", read_only: 1, in_list_view: 1 },
                { fieldname: "uom", fieldtype: "Data", read_only: 1 },
                { fieldname: "stock_uom", fieldtype: "Data", read_only: 1 },
                { fieldname: "is_scrap_item", fieldtype: "Check", read_only: 1 },
                { fieldname: "is_finished_item", fieldtype: "Check", read_only: 1 }
            ]
        }],
        primary_action_label: "Create Adjustment",
        
        primary_action() {
            if (!d.get_value("t_warehouse") || !d.get_value("s_warehouse")) {
                frappe.msgprint("Target and Source warehouse are mandatory");
                return;
            }


            let rows = d.fields_dict.items.grid.get_data();
            let changed_items = [];
            let branch = d.get_value("branch");
            let t_WH = d.get_value("t_warehouse");
            let s_WH = d.get_value("s_warehouse");

            rows.forEach(r => {
                const delta = flt(r.delta_qty);

                if (delta !== 0) {
                    changed_items.push({
                        item_code: r.item_code,
                        qty: delta,
                        is_scrap_item: cint(r.is_scrap_item),
                        is_finished_item: cint(r.is_finished_item)
                    });
                }
            });

            if (!changed_items.length) {
                frappe.msgprint("No quantity change detected");
                return;
            }


            frappe.call({
                method: "dt_gcfoods_customization.api.create_work_order_adjustments",
                args: {
                    work_order: frm.doc.name,
                    items: changed_items,
                    branch: d.get_value("branch"),
                    t_warehouse: t_WH,
                    s_warehouse: s_WH

                },
                callback(r) {
                    frappe.show_alert({ message: "Adjustment Posted", indicator: "green" });
                    d.hide();
                    frm.reload_doc();
                }
            });
        }


    });

    d.fields_dict.items.df.data = items;
    d.fields_dict.items.grid.refresh();

    let grid = d.fields_dict.items.grid;

    // base qty
    grid.df.data.forEach(r => {
        r._base_qty = flt(r.qty);
        r.delta_qty = 0;
    });

    if (frm.doc.custom_branch) {
        d.fields_dict.t_warehouse.get_query = function () {
            return { filters: { custom_branch: frm.doc.custom_branch } };
        };
        d.fields_dict.s_warehouse.get_query = function () {
            return { filters: { custom_branch: frm.doc.custom_branch } };
        };
    }

    prepare_base_quantities(grid);
    bind_scrap_qty_change(grid);
    d.show();
    d.set_value("t_warehouse", frm.doc.warehouse);
    d.set_value("s_warehouse", frm.doc.warehouse);

}

function prepare_base_quantities(grid) {
    grid._base_finished_qty = 0;
    grid._base_scrap_total = 0;

    grid.df.data.forEach(r => {
        r._base_qty = flt(r.qty);
        // r._touched = false;   //  TRACK INTENT

        if (cint(r.is_finished_item) === 1) {
            grid._base_finished_qty = flt(r.qty);
        }

        if (
            cint(r.is_scrap_item) === 1 &&
            !["PCS", "NOS"].includes((r.stock_uom || "").toUpperCase())
        ) {
            grid._base_scrap_total += flt(r.qty);
        }
    });
}

function bind_scrap_qty_change(grid) {

    let debounce_timer = null;
    grid.wrapper.off("change.scrap_calc");
    grid.wrapper.on(
        "change.scrap_calc",
        "input[data-fieldname='qty']",
        function () {

            const $row = $(this).closest(".grid-row");
            const docname = $row.attr("data-name");
            if (!docname) return;

            const row = grid.grid_rows_by_docname[docname]?.doc;
            if (!row) return;

            clearTimeout(debounce_timer);
            debounce_timer = setTimeout(() => {
                recalc_finished_from_scrap(grid);

                update_delta_qty(grid);
            }, 120);
        }
    );
}

function recalc_finished_from_scrap(grid) {

    let current_scrap_total = 0;
    let finished_row = null;
    
    grid.df.data.forEach(r => {
        if (
            cint(r.is_scrap_item) === 1 &&
            !["PCS", "NOS"].includes((r.stock_uom || "").toUpperCase())
        ) {
            current_scrap_total += flt(r.qty);
        }

        if (cint(r.is_finished_item) === 1) {
            finished_row = r;
        }
    });

    if (!finished_row) return;

    let new_qty = flt(
        grid._base_finished_qty +
        (grid._base_scrap_total - current_scrap_total),
        3
    );

    if (new_qty < 0) {
        frappe.msgprint("Finished quantity cannot be negative");
        return;
    }

    if (flt(finished_row.qty) !== new_qty) {
        finished_row.qty = new_qty;
        finished_row._touched = true;   //  SYSTEM CHANGE COUNTS
    }

    let idx = grid.df.data.indexOf(finished_row);
    if (idx > -1 && grid.grid_rows[idx]) {
        grid.grid_rows[idx].refresh_field("qty");
    }
}

function update_delta_qty(grid) {
    grid.df.data.forEach(r => {
        r.delta_qty = flt(r.qty) - flt(r._base_qty);
        let row = grid.grid_rows_by_docname[r.name];
        if (row) {
            row.refresh_field("delta_qty");
        }
    });
}


