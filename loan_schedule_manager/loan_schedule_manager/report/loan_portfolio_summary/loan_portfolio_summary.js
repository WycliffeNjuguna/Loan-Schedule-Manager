frappe.query_reports["Loan Portfolio Summary"] = {
    filters: [
        {
            fieldname: "as_of_date",
            label: __("As Of Date"),
            fieldtype: "Date",
            default: frappe.datetime.get_today(),
        },
        {
            fieldname: "status",
            label: __("Status"),
            fieldtype: "Select",
            options: "\nActive\nCompleted\nCancelled",
            default: "Active",
        },
        {
            fieldname: "customer_name",
            label: __("Customer (contains)"),
            fieldtype: "Data",
        },
    ],

    formatter(value, row, column, data, default_formatter) {
        value = default_formatter(value, row, column, data);

        if (column.fieldname === "overdue_lines" && data && data.overdue_lines > 0) {
            value = `<span style="color:var(--red-500);font-weight:600;">${data.overdue_lines} overdue</span>`;
        }

        if (column.fieldname === "status") {
            const colours = { Active: "blue", Completed: "green", Cancelled: "grey" };
            const c = colours[data?.status] || "grey";
            value = `<span class="indicator-pill ${c}">${data?.status}</span>`;
        }

        if (column.fieldname === "outstanding_amount" && data) {
            if (parseFloat(data.outstanding_amount) === 0) {
                value = `<span style="color:var(--green-500);">Paid off</span>`;
            }
        }

        return value;
    },
};
