// Copyright (c) 2026, Aakvatech and contributors
// For license information, please see license.txt

frappe.query_reports["Weekly Overtime attandance"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: "Gulled Industry Limited",
			reqd: 1,
		},
		{
			fieldname: "shift",
			label: __("Shift"),
			fieldtype: "Link",
			options: "Shift Type",
		},
		{
			fieldname: "employee",
			label: __("Employee"),
			fieldtype: "Link",
			options: "Employee",
		},
		{
			fieldname: "device_id",
			label: __("Device"),
			fieldtype: "Select",
			options: "\nbiscuit\nchaileo",
			default: "biscuit",
		},
	],
};