// Copyright (c) 2024, Aakvatech and contributors
// For license information, please see license.txt
/* eslint-disable */
frappe.query_reports["Gulled Industry Attandance Report"] = {
	"filters": [
		{
			"fieldname": "from_date",
			"label": __("From Date"),
			"fieldtype": "Date",
			"width": "150px",
			"reqd": 1,
			"default": frappe.datetime.month_start()
		},
		{
			"fieldname": "to_date",
			"label": __("To Date"),
			"fieldtype": "Date",
			"width": "150px",
			"reqd": 1,
			"default": frappe.datetime.month_end()
		},
		{
			"fieldname": "company",
			"label": __("Company"),
			"fieldtype": "Link",
			"options": "Company",
			"width": "150px",
			"reqd": 1,
			"default": frappe.defaults.get_user_default("company")
		},
		{
			"fieldname": "shift",
			"label": __("Shift"),
			"fieldtype": "Link",
			"options": "Shift Type",
			"width": "150px",
			"reqd": 0
		},
		{
			"fieldname": "employee",
			"label": __("Employee"),
			"fieldtype": "Link",
			"options": "Employee",
			"width": "150px",
			"reqd": 0
		},
		{
			"fieldname": "device_id",
			"label": __("Device"),
			"fieldtype": "Select",
			"options": "\nBiscuit\nChaileo",
			"width": "150px",
			"reqd": 0
		}
	]
};