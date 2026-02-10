# For license information, please see license.txt
import frappe
from datetime import datetime, timedelta
from frappe import msgprint, _
from frappe.utils.nestedset import get_descendants_of
from frappe.utils import getdate, get_time, flt

# Shift configuration - adjust shift names to match your system
SHIFT_CONFIG = {
    "Shift A": {"start": "06:00:00", "end": "18:00:00", "overtime_rate": 3500, "overnight": False, "early_start": "05:00:00"},
    "Shift B": {"start": "10:00:00", "end": "22:00:00", "overtime_rate": 4000, "overnight": False, "early_start": None},
    "Shift C": {"start": "18:00:00", "end": "06:00:00", "overtime_rate": 4000, "overnight": True, "early_start": None},
}

# Detection ranges for unassigned shifts (based on checkin time)
SHIFT_DETECTION = [
    {"shift": "Shift A", "start_hour": 5, "end_hour": 9}, # 05:00 - 09:59
    {"shift": "Shift B", "start_hour": 10, "end_hour": 16}, # 10:00 - 16:59
    {"shift": "Shift C", "start_hour": 17, "end_hour": 23}, # 17:00 - 23:59
]

EXTRA_OVERTIME_RATE = 1000 # TSH per hour above threshold

def execute(filters=None):
    # Validate required filters
    if not filters:
        filters = frappe._dict()

    # Ensure required filters are provided
    if not filters.get("from_date") or not filters.get("to_date"):
        frappe.throw(_("From Date and To Date are required"))

    if not filters.get("company"):
        frappe.throw(_("Company is required"))

    conditions, filters = get_conditions(filters)
    columns = get_columns()
    data = get_attendance_data(conditions, filters)

    if not data:
        msgprint(
            "No Record found for the filters From Date: {0}, To Date: {1}, Company: {2}, Shift: {3}, and Employee: {4}. "
            "Please set different filters and try again.".format(
                frappe.bold(filters.from_date),
                frappe.bold(filters.to_date),
                frappe.bold(filters.company),
                frappe.bold(filters.get("shift", "")),
                frappe.bold(filters.get("employee", "")),
            )
        )

    return columns, data

def get_columns():
    """Return simplified columns for the report."""
    return [
        {"fieldname": "employee", "label": _("Employee No"), "fieldtype": "Link", "options": "Employee", "width": 120},
        {"fieldname": "employee_name", "label": _("Employee Name"), "fieldtype": "Data", "width": 150},
        {"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
        {"fieldname": "shift", "label": _("Shift Type"), "fieldtype": "Data", "width": 100},
        {"fieldname": "first_checkin", "label": _("First Checkin"), "fieldtype": "Time", "width": 100},
        {"fieldname": "last_checkout", "label": _("Last Checkout"), "fieldtype": "Time", "width": 100},
        {"fieldname": "total_worked_hours", "label": _("Total Worked Hours"), "fieldtype": "Data", "width": 130},
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
        {"fieldname": "overtime_tsh", "label": _("Overtime TSH"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "extra_overtime", "label": _("Extra Overtime"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "total_overtime", "label": _("Total Overtime"), "fieldtype": "Currency", "width": 120},
    ]

def get_conditions(filters):
    """Build SQL conditions from filters (shift filter applied in Python later)."""
    conditions = ""
    if filters.get("from_date"):
        conditions += " AND DATE(chec.time) >= %(from_date)s"
    if filters.get("to_date"):
        conditions += " AND DATE(chec.time) <= %(to_date)s"
    if filters.get("company"):
        conditions += " AND emp.company = %(company)s"
    # Shift filter removed from SQL - will be applied in Python after shift determination
    if filters.get("employee"):
        conditions += " AND chec.employee = %(employee)s"
    if filters.get("device_id"):
        conditions += " AND chec.device_id = %(device_id)s"
    return conditions, filters

def get_attendance_data(conditions, filters):
    """Get attendance data with proper checkin/checkout matching."""
   
    # Get all checkin records
    checkin_records = get_all_checkins(conditions, filters)
   
    # Get all checkout records
    checkout_records = get_all_checkouts(conditions, filters)
   
    # Get shift assignments
    shift_assignments = get_shift_assignments(filters)
   
    # Get employee default shifts
    employee_defaults = get_employee_defaults(filters)
   
    # Build attendance rows by matching checkin with checkout
    attendance_data = []
   
    # Group checkins by employee and date
    checkins_by_emp_date = {}
    for rec in checkin_records:
        key = (rec.employee, str(rec.date))
        if key not in checkins_by_emp_date:
            checkins_by_emp_date[key] = []
        checkins_by_emp_date[key].append(rec)
   
    # Group checkouts by employee and date
    checkouts_by_emp_date = {}
    for rec in checkout_records:
        key = (rec.employee, str(rec.date))
        if key not in checkouts_by_emp_date:
            checkouts_by_emp_date[key] = []
        checkouts_by_emp_date[key].append(rec)
   
    # Process all unique employee-date combinations
    processed_keys = set()
    all_keys = set(checkins_by_emp_date.keys()) | set(checkouts_by_emp_date.keys())
   
    for key in all_keys:
        employee, date_str = key
        date_obj = getdate(date_str)

        # Get employee info
        emp_info = employee_defaults.get(employee, {})
        employee_name = emp_info.get("employee_name", "")
        default_shift = emp_info.get("default_shift", "")

        # Get checkins for this employee-date
        checkins = checkins_by_emp_date.get(key, [])
        first_checkin = min([c.checkin_time for c in checkins]) if checkins else None

        # PRIORITY: Detect shift from actual checkin time (most reliable, reflects real behavior)
        # This ensures employees working a different shift than assigned are calculated correctly
        assigned_shift = detect_shift_from_time(first_checkin) if first_checkin else None

        # If shift detection fails, fall back to assigned shift or default
        if not assigned_shift:
            assigned_shift = get_shift_for_date(employee, date_obj, shift_assignments, default_shift)

        # Determine checkout date based on shift type
        checkout_date_str = date_str
        is_overnight = assigned_shift and SHIFT_CONFIG.get(assigned_shift, {}).get("overnight")

        if is_overnight:
            # For overnight shift (e.g., Shift C: 18:00-06:00), checkout is next day
            next_date = date_obj + timedelta(days=1)
            checkout_date_str = str(next_date)

        # Get checkouts based on determined checkout date
        checkout_key = (employee, checkout_date_str)
        checkouts = checkouts_by_emp_date.get(checkout_key, [])

        last_checkout = max([c.checkout_time for c in checkouts]) if checkouts else None

        # Validate/refine shift detection using checkout time
        # If checkin is evening but no checkout found, check if early morning checkout on same date (night shift)
        if first_checkin and not last_checkout and assigned_shift == "Shift B":
            # Check for early morning checkout on same date (might be night shift employee)
            same_day_checkouts = checkouts_by_emp_date.get(key, [])
            if same_day_checkouts:
                early_checkout = min([c.checkout_time for c in same_day_checkouts])
                checkout_hour = get_time(str(early_checkout)).hour
                # If checkout is early morning (0-6), likely a night shift
                if checkout_hour < 6:
                    assigned_shift = "Shift C"
                    is_overnight = True
                    last_checkout = early_checkout
       
        # Calculate total worked hours (pass checkout date for overnight shifts)
        checkout_date_obj = getdate(checkout_date_str) if checkout_date_str != date_str else date_obj
        total_hours = calculate_worked_hours(first_checkin, last_checkout, date_obj, checkout_date_obj)

        # Determine status
        status = get_status(first_checkin, last_checkout, total_hours)

        # Calculate overtime
        overtime_tsh, extra_overtime, total_overtime = calculate_overtime(assigned_shift, total_hours)

        # Format hours for display
        hours_display = format_hours(total_hours) if total_hours else ""

        # Skip if this specific date-key was already processed as a checkin date
        if key in processed_keys:
            continue

        # Mark ONLY the current checkin date as processed
        # DO NOT mark the checkout date as processed if it might have its own checkin
        processed_keys.add(key)

        # CRITICAL: Only include records with BOTH checkin AND checkout data
        # Missing checkin or checkout = incomplete record, should NOT be included in report
        # This prevents payment errors and ensures accurate daily calculations
        if not first_checkin or not last_checkout:
            continue

        # Apply shift filter in Python (after shift has been determined)
        if filters.get("shift") and assigned_shift != filters.get("shift"):
            continue

        row = {
            "employee": employee,
            "employee_name": employee_name,
            "date": date_obj,
            "shift": assigned_shift or "",
            "first_checkin": str(first_checkin) if first_checkin else "",
            "last_checkout": str(last_checkout) if last_checkout else "",
            "total_worked_hours": hours_display,
            "status": status,
            "overtime_tsh": overtime_tsh,
            "extra_overtime": extra_overtime,
            "total_overtime": total_overtime,
        }

        attendance_data.append(row)

    # Sort by date and employee
    attendance_data.sort(key=lambda x: (x["date"], x["employee"]))

    # Convert to list of lists
    return [[row["employee"], row["employee_name"], row["date"], row["shift"],
             row["first_checkin"], row["last_checkout"], row["total_worked_hours"],
             row["status"], row["overtime_tsh"], row["extra_overtime"], row["total_overtime"]]
            for row in attendance_data]

def get_all_checkins(conditions, filters):
    """Get all checkin records without shift assignment join to avoid duplicates."""
    data = frappe.db.sql("""
        SELECT
            chec.employee AS employee,
            chec.employee_name AS employee_name,
            DATE(chec.time) AS date,
            TIME(chec.time) AS checkin_time,
            chec.time AS full_datetime
        FROM `tabEmployee Checkin` chec
            INNER JOIN `tabEmployee` emp ON emp.name = chec.employee
        WHERE chec.log_type = "IN" {conditions}
        ORDER BY chec.time ASC
    """.format(conditions=conditions), filters, as_dict=1)
    return data

def get_all_checkouts(conditions, filters):
    """Get all checkout records, extending date range by 1 day for overnight shifts."""
    # Extend the date range by 1 day for checkout to capture overnight shifts
    extended_filters = filters.copy()
    if extended_filters.get("to_date"):
        to_date = getdate(extended_filters["to_date"])
        extended_filters["to_date"] = str(to_date + timedelta(days=1))

    # Build conditions - shift filter removed, will be applied in Python
    checkout_conditions = ""
    if filters.get("from_date"):
        checkout_conditions += " AND DATE(chec.time) >= %(from_date)s"
    if extended_filters.get("to_date"):
        checkout_conditions += " AND DATE(chec.time) <= %(to_date)s"
    if filters.get("company"):
        checkout_conditions += " AND emp.company = %(company)s"
    if filters.get("employee"):
        checkout_conditions += " AND chec.employee = %(employee)s"
    if filters.get("device_id"):
        checkout_conditions += " AND chec.device_id = %(device_id)s"

    data = frappe.db.sql("""
        SELECT
            chec.employee AS employee,
            chec.employee_name AS employee_name,
            DATE(chec.time) AS date,
            TIME(chec.time) AS checkout_time,
            chec.time AS full_datetime
        FROM `tabEmployee Checkin` chec
            INNER JOIN `tabEmployee` emp ON emp.name = chec.employee
        WHERE chec.log_type = "OUT" {conditions}
        ORDER BY chec.time ASC
    """.format(conditions=checkout_conditions), extended_filters, as_dict=1)
    return data

def get_shift_assignments(filters):
    """Get shift assignments for the date range."""
    from_date = filters.get("from_date")
    to_date = filters.get("to_date")
   
    data = frappe.db.sql("""
        SELECT
            sha.employee,
            sha.shift_type,
            sha.start_date,
            sha.end_date
        FROM `tabShift Assignment` sha
        WHERE sha.docstatus = 1
            AND sha.start_date <= %(to_date)s
            AND (sha.end_date IS NULL OR sha.end_date >= %(from_date)s)
    """, {"from_date": from_date, "to_date": to_date}, as_dict=1)
   
    # Group by employee
    assignments = {}
    for row in data:
        if row.employee not in assignments:
            assignments[row.employee] = []
        assignments[row.employee].append(row)
   
    return assignments

def get_employee_defaults(filters):
    """Get employee default shifts and names."""
    conditions = ""
    if filters.get("company"):
        conditions += " AND emp.company = %(company)s"
    if filters.get("employee"):
        conditions += " AND emp.name = %(employee)s"
   
    data = frappe.db.sql("""
        SELECT
            emp.name AS employee,
            emp.employee_name,
            emp.default_shift
        FROM `tabEmployee` emp
        WHERE emp.status = 'Active' {conditions}
    """.format(conditions=conditions), filters, as_dict=1)
   
    return {row.employee: row for row in data}

def get_shift_for_date(employee, date, shift_assignments, default_shift):
    """Get the shift assigned to an employee for a specific date."""
    assignments = shift_assignments.get(employee, [])
   
    for assignment in assignments:
        start = assignment.start_date
        end = assignment.end_date or date
        if start <= date <= end:
            return assignment.shift_type
   
    return default_shift or None

def detect_shift_from_time(checkin_time):
    """Detect shift based on checkin time when no shift is assigned."""
    if not checkin_time:
        return None
   
    try:
        time_obj = get_time(str(checkin_time))
        hour = time_obj.hour
       
        for detection in SHIFT_DETECTION:
            if detection["start_hour"] <= hour <= detection["end_hour"]:
                return detection["shift"]
    except Exception:
        pass
   
    return None

def calculate_worked_hours(first_checkin, last_checkout, checkin_date, checkout_date):
    """
    Calculate total worked hours with precise date handling.

    Args:
        first_checkin: Time object of first check-in
        last_checkout: Time object of last check-out
        checkin_date: Date object of check-in
        checkout_date: Date object of check-out (may be next day for overnight shifts)

    Returns:
        Float: Total hours worked, or None if calculation fails
    """
    if not first_checkin or not last_checkout:
        return None

    try:
        checkin_time = get_time(str(first_checkin))
        checkout_time = get_time(str(last_checkout))

        # Create precise datetime objects using provided dates
        checkin_dt = datetime.combine(checkin_date, checkin_time)
        checkout_dt = datetime.combine(checkout_date, checkout_time)

        # Calculate time difference
        delta = checkout_dt - checkin_dt

        # Ensure we don't have negative hours (data quality issue)
        if delta.total_seconds() < 0:
            frappe.log_error(
                f"Negative hours detected: Checkin {checkin_dt}, Checkout {checkout_dt}",
                "Attendance Calculation Error"
            )
            return None

        total_hours = delta.total_seconds() / 3600

        return flt(total_hours, 2)
    except Exception as e:
        frappe.log_error(f"Error calculating hours: {e}", "Attendance Calculation Error")
        return None

def format_hours(total_hours):
    """Format hours as HH:MM:SS string."""
    if total_hours is None:
        return ""
   
    hours = int(total_hours)
    minutes = int((total_hours - hours) * 60)
    seconds = int(((total_hours - hours) * 60 - minutes) * 60)
   
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def get_status(first_checkin, last_checkout, total_hours):
    """Determine attendance status based on checkin, checkout and hours."""
    if not first_checkin:
        return "Missing Checkin"
    if not last_checkout:
        return "Missing Checkout"
    if total_hours is None:
        return "Error"
   
    if total_hours >= 12:
        return "Full Day"
    elif total_hours >= 8:
        return "Partial"
    else:
        return "Short Day"

def calculate_overtime(shift, total_hours):
    """
    Calculate overtime based on shift type and hours worked.
   
    Rules:
    - If hours >= 12: Get base overtime (3500 for Shift A, 4000 for Shift B/C)
                      + extra overtime = (hours - 12) * 1000
    - If 8 < hours < 12: No base overtime, but extra overtime = (hours - 8) * 1000
    - If hours <= 8: No overtime
    """
    if not total_hours or total_hours <= 0:
        return 0, 0, 0
   
    shift_config = SHIFT_CONFIG.get(shift, {})
    base_overtime_rate = shift_config.get("overtime_rate", 0)
   
    overtime_tsh = 0
    extra_overtime = 0
   
    if total_hours >= 12:
        # Full day - get base overtime plus extra
        overtime_tsh = base_overtime_rate
        extra_hours = total_hours - 12
        if extra_hours > 0:
            extra_overtime = flt(extra_hours * EXTRA_OVERTIME_RATE, 0)
    elif total_hours > 8:
        # Partial day - no base overtime but get extra overtime
        extra_hours = total_hours - 8
        extra_overtime = flt(extra_hours * EXTRA_OVERTIME_RATE, 0)
    # else: hours <= 8, no overtime
   
    total_overtime = overtime_tsh + extra_overtime
   
    return overtime_tsh, extra_overtime, total_overtime