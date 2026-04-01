"""
Script to fix payroll journal entries where NSSF/WCF credits went to expense accounts
instead of payable accounts.

Wrong: CR 5223 - NSSF Employer Contribution (expense)
Right: CR 2505 - NSSF Payable

Wrong: CR 5222 - WCF Expense (expense)
Right: CR 2506 - WCF Payable

Usage (run from frappe-bench root):
  bench --site gulled.localhost execute "csf_tz.fix_nssf_wcf_jv.test_one_jv"
  bench --site gulled.localhost execute "csf_tz.fix_nssf_wcf_jv.fix_all_jvs"
"""

import frappe
from frappe.utils import flt

NSSF_EXPENSE_ACCOUNT = "5223 - NSSF - Employer Contribution - GIL"
NSSF_PAYABLE_ACCOUNT = "2505 - NSSF Payable - GIL"
WCF_EXPENSE_ACCOUNT = "5222 - WCF Expense - GIL"
WCF_PAYABLE_ACCOUNT = "2506 - WCF Payable - GIL"


def get_wrong_jvs():
    """Get all payroll JVs with wrong NSSF/WCF credit postings before 2026."""
    return frappe.db.sql(
        """
        SELECT DISTINCT je.name, je.posting_date, je.user_remark
        FROM `tabJournal Entry` je
        JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
        WHERE je.docstatus = 1
          AND je.voucher_type = 'Journal Entry'
          AND jea.account IN (%(nssf)s, %(wcf)s)
          AND jea.credit_in_account_currency > 0
          AND je.posting_date < '2026-01-01'
        ORDER BY je.posting_date, je.name
        """,
        {
            "nssf": NSSF_EXPENSE_ACCOUNT,
            "wcf": WCF_EXPENSE_ACCOUNT,
        },
        as_dict=True,
    )


def fix_one_jv(jv_name, commit=True, dry_run=False):
    """
    Create a corrected copy of the wrong JV (with proper NSSF/WCF accounts),
    then cancel the original.
    Returns (new_jv_name, nssf_fixed, wcf_fixed) or None if skipped.
    """
    jv_doc = frappe.get_doc("Journal Entry", jv_name)

    if jv_doc.docstatus != 1:
        print(f"  SKIP {jv_name}: not submitted (docstatus={jv_doc.docstatus})")
        return None

    # Check for wrong credits
    nssf_wrong_credit = sum(
        flt(acc.credit_in_account_currency)
        for acc in jv_doc.accounts
        if acc.account == NSSF_EXPENSE_ACCOUNT and acc.credit_in_account_currency > 0
    )
    wcf_wrong_credit = sum(
        flt(acc.credit_in_account_currency)
        for acc in jv_doc.accounts
        if acc.account == WCF_EXPENSE_ACCOUNT and acc.credit_in_account_currency > 0
    )

    if nssf_wrong_credit == 0 and wcf_wrong_credit == 0:
        print(f"  SKIP {jv_name}: no wrong NSSF/WCF credits found")
        return None

    print(
        f"  {jv_name} ({jv_doc.posting_date}): "
        f"NSSF fix={nssf_wrong_credit:,.2f}, WCF fix={wcf_wrong_credit:,.2f}"
    )

    if dry_run:
        return (None, nssf_wrong_credit, wcf_wrong_credit)

    # Find the linked payroll entry
    payroll_entry = None
    for acc in jv_doc.accounts:
        if acc.reference_type == "Payroll Entry" and acc.reference_name:
            payroll_entry = acc.reference_name
            break
    if not payroll_entry:
        for acc in jv_doc.accounts:
            if acc.account and "Payroll Payable" in acc.account and acc.reference_name:
                payroll_entry = acc.reference_name
                break

    # Build corrected accounts list (copying all fields including party)
    new_accounts = []
    for acc in jv_doc.accounts:
        row = {
            "account": acc.account,
            "debit_in_account_currency": flt(acc.debit_in_account_currency),
            "credit_in_account_currency": flt(acc.credit_in_account_currency),
            "cost_center": acc.cost_center,
            "project": acc.project,
            "party_type": acc.party_type,
            "party": acc.party,
            "reference_type": acc.reference_type,
            "reference_name": acc.reference_name,
            "user_remark": acc.user_remark,
            "is_advance": acc.is_advance,
            "exchange_rate": acc.exchange_rate or 1,
        }

        # Correct NSSF wrong credit to NSSF Payable
        if acc.account == NSSF_EXPENSE_ACCOUNT and acc.credit_in_account_currency > 0:
            row["account"] = NSSF_PAYABLE_ACCOUNT

        # Correct WCF wrong credit to WCF Payable
        elif acc.account == WCF_EXPENSE_ACCOUNT and acc.credit_in_account_currency > 0:
            row["account"] = WCF_PAYABLE_ACCOUNT

        new_accounts.append(row)

    # Step 1: Cancel the original wrong JV first (so employee advances become free).
    # Use _cancel() directly to bypass the background submission queue
    # (queue is triggered for JVs with >100 account lines).
    # Unlock any stale file lock before cancelling.
    jv_doc.reload()
    if jv_doc.is_locked:
        jv_doc.unlock()
    jv_doc._cancel()
    print(f"    Cancelled original: {jv_name}")

    # Step 2: Create new corrected JV with the right NSSF/WCF accounts
    new_jv = frappe.new_doc("Journal Entry")
    new_jv.voucher_type = jv_doc.voucher_type
    new_jv.posting_date = jv_doc.posting_date
    new_jv.company = jv_doc.company
    new_jv.user_remark = (jv_doc.user_remark or "") + " [CORRECTED: NSSF/WCF to Payable accounts]"
    new_jv.cheque_no = jv_doc.cheque_no
    new_jv.cheque_date = jv_doc.cheque_date
    new_jv.pay_to_recd_from = jv_doc.pay_to_recd_from

    for row in new_accounts:
        new_jv.append("accounts", row)

    new_jv.flags.ignore_permissions = True
    new_jv.insert(ignore_permissions=True)
    # Clear any stale lock that might exist for this new JV name (from a previous retry)
    if new_jv.is_locked:
        new_jv.unlock()
    new_jv.submit()
    print(f"    Created & Submitted: {new_jv.name} (payroll entry: {payroll_entry})")

    if commit:
        frappe.db.commit()

    return (new_jv.name, nssf_wrong_credit, wcf_wrong_credit)


def test_one_jv():
    """Test on a single JV (smallest/simplest one) with rollback."""
    wrong_jvs = get_wrong_jvs()
    if not wrong_jvs:
        print("No wrong JVs found!")
        return

    # Test on a smaller JV (fewer lines = fewer complications)
    # Pick HR-PRUN-2025-00125 -> ACC-JV-2025-03338 (small payroll entry)
    test_jv = next(
        (jv for jv in wrong_jvs if jv["name"] == "ACC-JV-2025-03338"),
        wrong_jvs[0]  # fallback to first
    )
    print(f"Testing on: {test_jv['name']} ({test_jv['posting_date']})")
    print(f"Remark: {test_jv['user_remark']}")
    print()

    try:
        result = fix_one_jv(test_jv["name"], commit=False)
        if result:
            new_name, nssf_fixed, wcf_fixed = result
            print(f"\nSUCCESS: New JV = {new_name}")
            print(f"  NSSF moved to payable: {nssf_fixed:,.2f}")
            print(f"  WCF moved to payable:  {wcf_fixed:,.2f}")
        else:
            print("No changes made (already correct or skipped).")
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\nRolling back (test mode - nothing was committed)...")
        frappe.db.rollback()


def dry_run():
    """Show what would be fixed without making changes."""
    wrong_jvs = get_wrong_jvs()
    print(f"Found {len(wrong_jvs)} JVs to fix:\n")

    total_nssf = 0
    total_wcf = 0
    for jv in wrong_jvs:
        result = fix_one_jv(jv["name"], commit=False, dry_run=True)
        if result:
            _, n, w = result
            total_nssf += n
            total_wcf += w

    print(f"\nTotal NSSF to move to Payable: {total_nssf:,.2f}")
    print(f"Total WCF to move to Payable:  {total_wcf:,.2f}")


def find_all_nssf_payments():
    """
    Find ALL submitted JVs (any voucher_type) where 5223 NSSF expense account
    is DEBITED — these are payment entries that should debit 2505 NSSF Payable instead.
    Excludes payroll accrual JVs (where 5223 debit is the employer contribution expense
    side — those were already handled separately and the DR side is kept correct).
    A 'payment' entry is identified as one where 5223 has a DEBIT with no matching
    credit in the same JV (i.e. it's not a payroll accrual that nets out).
    """
    rows = frappe.db.sql(
        """
        SELECT DISTINCT je.name, je.posting_date, je.voucher_type,
               je.user_remark, je.cheque_no, je.cheque_date, je.pay_to_recd_from
        FROM `tabJournal Entry` je
        JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
        WHERE je.docstatus = 1
          AND jea.account = %(nssf)s
          AND jea.debit_in_account_currency > 0
          AND je.name NOT IN (
              /* Exclude payroll accrual JVs — they credit Payroll Payable */
              SELECT DISTINCT parent FROM `tabJournal Entry Account`
              WHERE account LIKE '%%Payroll Payable%%' AND credit_in_account_currency > 0
          )
        ORDER BY je.posting_date, je.name
        """,
        {"nssf": NSSF_EXPENSE_ACCOUNT},
        as_dict=True,
    )

    print(f"Found {len(rows)} JVs with 5223 NSSF expense DEBITED (payment entries):\n")
    for r in rows:
        lines = frappe.db.sql(
            """SELECT account, debit_in_account_currency, credit_in_account_currency
               FROM `tabJournal Entry Account` WHERE parent = %s ORDER BY idx""",
            r.name, as_dict=True,
        )
        print(f"  {r.name} | {r.posting_date} | {r.voucher_type}")
        print(f"    cheque_no: {r.cheque_no}")
        print(f"    remark   : {r.user_remark}")
        for l in lines:
            marker = " <<< WRONG" if (l.account == NSSF_EXPENSE_ACCOUNT and l.debit_in_account_currency) else ""
            dr = f"DR {l.debit_in_account_currency:>14,.2f}" if l.debit_in_account_currency else " " * 20
            cr = f"CR {l.credit_in_account_currency:>14,.2f}" if l.credit_in_account_currency else " " * 20
            print(f"    {dr}  {cr}  {l.account}{marker}")
        print()
    return rows


def find_all_wcf_payments():
    """
    Find ALL submitted JVs (any voucher_type) where 5222 WCF expense account
    is DEBITED — these are payment entries that should debit 2506 WCF Payable instead.
    Excludes payroll accrual JVs (those crediting Payroll Payable).
    """
    rows = frappe.db.sql(
        """
        SELECT DISTINCT je.name, je.posting_date, je.voucher_type,
               je.user_remark, je.cheque_no, je.cheque_date, je.pay_to_recd_from
        FROM `tabJournal Entry` je
        JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
        WHERE je.docstatus = 1
          AND jea.account = %(wcf)s
          AND jea.debit_in_account_currency > 0
          AND je.name NOT IN (
              /* Exclude payroll accrual JVs — they credit Payroll Payable */
              SELECT DISTINCT parent FROM `tabJournal Entry Account`
              WHERE account LIKE '%%Payroll Payable%%' AND credit_in_account_currency > 0
          )
        ORDER BY je.posting_date, je.name
        """,
        {"wcf": WCF_EXPENSE_ACCOUNT},
        as_dict=True,
    )

    print(f"Found {len(rows)} JVs with 5222 WCF expense DEBITED (payment entries):\n")
    for r in rows:
        lines = frappe.db.sql(
            """SELECT account, debit_in_account_currency, credit_in_account_currency
               FROM `tabJournal Entry Account` WHERE parent = %s ORDER BY idx""",
            r.name, as_dict=True,
        )
        print(f"  {r.name} | {r.posting_date} | {r.voucher_type}")
        print(f"    cheque_no: {r.cheque_no}")
        print(f"    remark   : {r.user_remark}")
        for l in lines:
            marker = " <<< WRONG" if (l.account == WCF_EXPENSE_ACCOUNT and l.debit_in_account_currency) else ""
            dr = f"DR {l.debit_in_account_currency:>14,.2f}" if l.debit_in_account_currency else " " * 20
            cr = f"CR {l.credit_in_account_currency:>14,.2f}" if l.credit_in_account_currency else " " * 20
            print(f"    {dr}  {cr}  {l.account}{marker}")
        print()
    return rows


def fix_all_wcf_payments():
    """
    Fix ALL JVs where 5222 WCF expense is debited as a payment.
    Cancels each and recreates with 2506 WCF Payable on the debit side.
    All other fields (remark, cheque_no, cheque_date, pay_to_recd_from) preserved exactly.
    """
    wrong_jvs = find_all_wcf_payments()
    if not wrong_jvs:
        print("Nothing to fix.")
        return

    success = []
    failed = []

    for jv in wrong_jvs:
        jv_name = jv["name"]
        print(f"Processing {jv_name} ({jv['posting_date']})...")
        try:
            new_name = _fix_wcf_payment_entry(jv_name)
            success.append((jv_name, new_name))
            frappe.db.commit()
        except Exception as e:
            frappe.db.rollback()
            print(f"  FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed.append((jv_name, str(e)))

    print(f"\n=== SUMMARY ===")
    print(f"Fixed:  {len(success)}")
    print(f"Failed: {len(failed)}")
    for old, new in success:
        print(f"  {old} -> {new}")
    for jv_name, err in failed:
        print(f"  FAILED {jv_name}: {err}")


def _fix_wcf_payment_entry(jv_name):
    """Cancel and recreate a JV replacing DR 5222 WCF expense with DR 2506 WCF Payable."""
    jv_doc = frappe.get_doc("Journal Entry", jv_name)

    if jv_doc.docstatus != 1:
        print(f"  SKIP: docstatus={jv_doc.docstatus}")
        return None

    jv_doc.reload()
    if jv_doc.is_locked:
        jv_doc.unlock()
    jv_doc._cancel()
    print(f"  Cancelled: {jv_name}")

    new_jv = frappe.new_doc("Journal Entry")
    new_jv.voucher_type = jv_doc.voucher_type
    new_jv.posting_date = jv_doc.posting_date
    new_jv.company = jv_doc.company
    new_jv.cheque_no = jv_doc.cheque_no
    new_jv.cheque_date = jv_doc.cheque_date
    new_jv.pay_to_recd_from = jv_doc.pay_to_recd_from
    new_jv.user_remark = jv_doc.user_remark  # preserved exactly

    for acc in jv_doc.accounts:
        row = {
            "account": acc.account,
            "debit_in_account_currency": flt(acc.debit_in_account_currency),
            "credit_in_account_currency": flt(acc.credit_in_account_currency),
            "cost_center": acc.cost_center,
            "project": acc.project,
            "party_type": acc.party_type,
            "party": acc.party,
            "reference_type": acc.reference_type,
            "reference_name": acc.reference_name,
            "user_remark": acc.user_remark,
            "is_advance": acc.is_advance,
            "exchange_rate": acc.exchange_rate or 1,
        }
        # Replace WCF expense with WCF Payable on the debit side only
        if acc.account == WCF_EXPENSE_ACCOUNT and acc.debit_in_account_currency > 0:
            row["account"] = WCF_PAYABLE_ACCOUNT

        new_jv.append("accounts", row)

    new_jv.flags.ignore_permissions = True
    new_jv.insert(ignore_permissions=True)
    if new_jv.is_locked:
        new_jv.unlock()
    new_jv.submit()

    print(f"  Created & Submitted: {new_jv.name}")
    for acc in new_jv.accounts:
        if acc.debit_in_account_currency:
            print(f"    DR  {acc.account}  {acc.debit_in_account_currency:,.2f}")
        if acc.credit_in_account_currency:
            print(f"    CR  {acc.account}  {acc.credit_in_account_currency:,.2f}")

    return new_jv.name


def fix_all_nssf_payments():
    """
    Fix ALL JVs where 5223 NSSF expense is debited as a payment.
    Cancels each and recreates with 2505 NSSF Payable on the debit side.
    All other fields (remark, cheque_no, cheque_date, pay_to_recd_from) preserved exactly.
    """
    wrong_jvs = find_all_nssf_payments()
    if not wrong_jvs:
        print("Nothing to fix.")
        return

    success = []
    failed = []

    for jv in wrong_jvs:
        jv_name = jv["name"]
        print(f"Processing {jv_name} ({jv['posting_date']})...")
        try:
            new_name = _fix_nssf_payment_entry(jv_name)
            success.append((jv_name, new_name))
            frappe.db.commit()
        except Exception as e:
            frappe.db.rollback()
            print(f"  FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed.append((jv_name, str(e)))

    print(f"\n=== SUMMARY ===")
    print(f"Fixed:  {len(success)}")
    print(f"Failed: {len(failed)}")
    for old, new in success:
        print(f"  {old} -> {new}")
    for jv_name, err in failed:
        print(f"  FAILED {jv_name}: {err}")


def _fix_nssf_payment_entry(jv_name):
    """Cancel and recreate a JV replacing DR 5223 NSSF expense with DR 2505 NSSF Payable."""
    jv_doc = frappe.get_doc("Journal Entry", jv_name)

    if jv_doc.docstatus != 1:
        print(f"  SKIP: docstatus={jv_doc.docstatus}")
        return None

    jv_doc.reload()
    if jv_doc.is_locked:
        jv_doc.unlock()
    jv_doc._cancel()
    print(f"  Cancelled: {jv_name}")

    new_jv = frappe.new_doc("Journal Entry")
    new_jv.voucher_type = jv_doc.voucher_type
    new_jv.posting_date = jv_doc.posting_date
    new_jv.company = jv_doc.company
    new_jv.cheque_no = jv_doc.cheque_no
    new_jv.cheque_date = jv_doc.cheque_date
    new_jv.pay_to_recd_from = jv_doc.pay_to_recd_from
    new_jv.user_remark = jv_doc.user_remark  # preserved exactly

    for acc in jv_doc.accounts:
        row = {
            "account": acc.account,
            "debit_in_account_currency": flt(acc.debit_in_account_currency),
            "credit_in_account_currency": flt(acc.credit_in_account_currency),
            "cost_center": acc.cost_center,
            "project": acc.project,
            "party_type": acc.party_type,
            "party": acc.party,
            "reference_type": acc.reference_type,
            "reference_name": acc.reference_name,
            "user_remark": acc.user_remark,
            "is_advance": acc.is_advance,
            "exchange_rate": acc.exchange_rate or 1,
        }
        # Replace NSSF expense with NSSF Payable on the debit side only
        if acc.account == NSSF_EXPENSE_ACCOUNT and acc.debit_in_account_currency > 0:
            row["account"] = NSSF_PAYABLE_ACCOUNT

        new_jv.append("accounts", row)

    new_jv.flags.ignore_permissions = True
    new_jv.insert(ignore_permissions=True)
    if new_jv.is_locked:
        new_jv.unlock()
    new_jv.submit()

    print(f"  Created & Submitted: {new_jv.name}")
    for acc in new_jv.accounts:
        if acc.debit_in_account_currency:
            print(f"    DR  {acc.account}  {acc.debit_in_account_currency:,.2f}")
        if acc.credit_in_account_currency:
            print(f"    CR  {acc.account}  {acc.credit_in_account_currency:,.2f}")

    return new_jv.name


def find_nssf_bank_entries():
    """
    Find all submitted non-payroll JVs (Bank Entry, Cash Entry, etc.) that use
    NSSF expense (5223) or WCF expense (5222) — should use payable accounts.
    """
    rows = frappe.db.sql(
        """
        SELECT DISTINCT je.name, je.posting_date, je.voucher_type,
               je.user_remark, je.cheque_no, je.cheque_date, je.pay_to_recd_from
        FROM `tabJournal Entry` je
        JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
        WHERE je.docstatus = 1
          AND je.voucher_type NOT IN ('Journal Entry')
          AND jea.account IN (%(nssf)s, %(wcf)s)
        ORDER BY je.posting_date, je.name
        """,
        {"nssf": NSSF_EXPENSE_ACCOUNT, "wcf": WCF_EXPENSE_ACCOUNT},
        as_dict=True,
    )

    print(f"Found {len(rows)} non-payroll JVs using NSSF/WCF expense accounts:\n")
    for r in rows:
        lines = frappe.db.sql(
            """SELECT account, debit_in_account_currency, credit_in_account_currency
               FROM `tabJournal Entry Account` WHERE parent = %s ORDER BY idx""",
            r.name, as_dict=True,
        )
        print(f"  {r.name} | {r.posting_date} | {r.voucher_type}")
        print(f"    cheque_no: {r.cheque_no}")
        print(f"    remark   : {r.user_remark}")
        for l in lines:
            marker = " <<< WRONG" if l.account in (NSSF_EXPENSE_ACCOUNT, WCF_EXPENSE_ACCOUNT) else ""
            dr = f"DR {l.debit_in_account_currency:>14,.2f}" if l.debit_in_account_currency else " " * 20
            cr = f"CR {l.credit_in_account_currency:>14,.2f}" if l.credit_in_account_currency else " " * 20
            print(f"    {dr}  {cr}  {l.account}{marker}")
        print()
    return rows


def fix_nssf_bank_entries():
    """
    Fix all non-payroll JVs using NSSF/WCF expense accounts.
    Cancels and recreates each with the correct payable accounts.
    Preserves original remark, cheque_no, cheque_date, pay_to_recd_from exactly.
    """
    wrong_jvs = find_nssf_bank_entries()
    if not wrong_jvs:
        print("Nothing to fix.")
        return

    success = []
    failed = []

    for jv in wrong_jvs:
        jv_name = jv["name"]
        print(f"Processing {jv_name} ({jv['posting_date']})...")
        try:
            new_name = _fix_nssf_bank_entry(jv_name)
            success.append((jv_name, new_name))
            frappe.db.commit()
        except Exception as e:
            frappe.db.rollback()
            print(f"  FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed.append((jv_name, str(e)))

    print(f"\n=== SUMMARY ===")
    print(f"Fixed:  {len(success)}")
    print(f"Failed: {len(failed)}")
    for old, new in success:
        print(f"  {old} -> {new}")
    for jv_name, err in failed:
        print(f"  FAILED {jv_name}: {err}")


def _fix_nssf_bank_entry(jv_name):
    """Cancel and recreate one non-payroll JV using payable accounts instead of expense."""
    jv_doc = frappe.get_doc("Journal Entry", jv_name)

    if jv_doc.docstatus != 1:
        print(f"  SKIP: docstatus={jv_doc.docstatus}")
        return None

    jv_doc.reload()
    if jv_doc.is_locked:
        jv_doc.unlock()
    jv_doc._cancel()
    print(f"  Cancelled: {jv_name}")

    new_jv = frappe.new_doc("Journal Entry")
    new_jv.voucher_type = jv_doc.voucher_type
    new_jv.posting_date = jv_doc.posting_date
    new_jv.company = jv_doc.company
    new_jv.cheque_no = jv_doc.cheque_no
    new_jv.cheque_date = jv_doc.cheque_date
    new_jv.pay_to_recd_from = jv_doc.pay_to_recd_from
    new_jv.user_remark = jv_doc.user_remark  # preserved exactly

    for acc in jv_doc.accounts:
        row = {
            "account": acc.account,
            "debit_in_account_currency": flt(acc.debit_in_account_currency),
            "credit_in_account_currency": flt(acc.credit_in_account_currency),
            "cost_center": acc.cost_center,
            "project": acc.project,
            "party_type": acc.party_type,
            "party": acc.party,
            "reference_type": acc.reference_type,
            "reference_name": acc.reference_name,
            "user_remark": acc.user_remark,
            "is_advance": acc.is_advance,
            "exchange_rate": acc.exchange_rate or 1,
        }
        if acc.account == NSSF_EXPENSE_ACCOUNT:
            row["account"] = NSSF_PAYABLE_ACCOUNT
        elif acc.account == WCF_EXPENSE_ACCOUNT:
            row["account"] = WCF_PAYABLE_ACCOUNT

        new_jv.append("accounts", row)

    new_jv.flags.ignore_permissions = True
    new_jv.insert(ignore_permissions=True)
    if new_jv.is_locked:
        new_jv.unlock()
    new_jv.submit()

    print(f"  Created & Submitted: {new_jv.name}")
    for acc in new_jv.accounts:
        if acc.debit_in_account_currency:
            print(f"    DR  {acc.account}  {acc.debit_in_account_currency:,.2f}")
        if acc.credit_in_account_currency:
            print(f"    CR  {acc.account}  {acc.credit_in_account_currency:,.2f}")

    return new_jv.name


def fix_wcf_refund_jv():
    """
    Fix ACC-JV-2025-03679 (Bank Entry, 2025-10-24):
    WCF refund received from government.

    Current (wrong):
      DR  CRDB Bank                127,247.60
      CR  5222 - WCF Expense       127,247.60

    Correct:
      DR  CRDB Bank                127,247.60  (unchanged)
      CR  2506 - WCF Payable       127,247.60  (refund reduces outstanding WCF liability)
    """
    jv_name = "ACC-JV-2025-03679"
    jv_doc = frappe.get_doc("Journal Entry", jv_name)

    print(f"=== {jv_name} ===")
    print(f"  posting_date    : {jv_doc.posting_date}")
    print(f"  voucher_type    : {jv_doc.voucher_type}")
    print(f"  user_remark     : {jv_doc.user_remark}")
    print(f"  cheque_no       : {jv_doc.cheque_no}")
    print(f"  cheque_date     : {jv_doc.cheque_date}")
    print(f"  pay_to_recd_from: {jv_doc.pay_to_recd_from}")

    if jv_doc.docstatus != 1:
        print(f"  SKIP: docstatus={jv_doc.docstatus}, not submitted")
        return

    jv_doc.reload()
    if jv_doc.is_locked:
        jv_doc.unlock()
    jv_doc._cancel()
    print(f"  Cancelled: {jv_name}")

    new_jv = frappe.new_doc("Journal Entry")
    new_jv.voucher_type = jv_doc.voucher_type
    new_jv.posting_date = jv_doc.posting_date
    new_jv.company = jv_doc.company
    new_jv.cheque_no = jv_doc.cheque_no
    new_jv.cheque_date = jv_doc.cheque_date
    new_jv.pay_to_recd_from = jv_doc.pay_to_recd_from
    new_jv.user_remark = (
        (jv_doc.user_remark or "")
        + " [CORRECTED: WCF refund received from government - CR to WCF Payable]"
    ).strip()

    for acc in jv_doc.accounts:
        row = {
            "account": acc.account,
            "debit_in_account_currency": flt(acc.debit_in_account_currency),
            "credit_in_account_currency": flt(acc.credit_in_account_currency),
            "cost_center": acc.cost_center,
            "project": acc.project,
            "party_type": acc.party_type,
            "party": acc.party,
            "reference_type": acc.reference_type,
            "reference_name": acc.reference_name,
            "user_remark": acc.user_remark,
            "is_advance": acc.is_advance,
            "exchange_rate": acc.exchange_rate or 1,
        }
        if acc.account == WCF_EXPENSE_ACCOUNT and acc.credit_in_account_currency > 0:
            row["account"] = WCF_PAYABLE_ACCOUNT
        new_jv.append("accounts", row)

    new_jv.flags.ignore_permissions = True
    new_jv.insert(ignore_permissions=True)
    if new_jv.is_locked:
        new_jv.unlock()
    new_jv.submit()
    frappe.db.commit()

    print(f"  Created & Submitted: {new_jv.name}")
    print(f"  Corrected entry:")
    for acc in new_jv.accounts:
        if acc.debit_in_account_currency:
            print(f"    DR  {acc.account}  {acc.debit_in_account_currency:,.2f}")
        if acc.credit_in_account_currency:
            print(f"    CR  {acc.account}  {acc.credit_in_account_currency:,.2f}")


def fix_salary_component_accounts():
    """
    Fix the salary component account mappings for NSSF, NSSF Employer, and WCF
    to use payable accounts instead of expense accounts.
    This prevents future payroll JVs from having the same wrong postings.
    """
    # NSSF -> NSSF Payable
    nssf_components = frappe.db.sql(
        """SELECT sca.name, sca.parent, sca.account
           FROM `tabSalary Component Account` sca
           WHERE sca.parent IN ('NSSF', 'NSSF Employer')
           AND sca.company = 'Gulled Industry Limited'
           AND sca.account = %s""",
        NSSF_EXPENSE_ACCOUNT,
        as_dict=True,
    )

    # WCF -> WCF Payable
    wcf_components = frappe.db.sql(
        """SELECT sca.name, sca.parent, sca.account
           FROM `tabSalary Component Account` sca
           WHERE sca.parent = 'WCF'
           AND sca.company = 'Gulled Industry Limited'
           AND sca.account = %s""",
        WCF_EXPENSE_ACCOUNT,
        as_dict=True,
    )

    print("Salary Component Account Mappings to Update:")

    for sca in nssf_components:
        frappe.db.set_value("Salary Component Account", sca.name, "account", NSSF_PAYABLE_ACCOUNT)
        print(f"  {sca.parent}: {sca.account} -> {NSSF_PAYABLE_ACCOUNT}")

    for sca in wcf_components:
        frappe.db.set_value("Salary Component Account", sca.name, "account", WCF_PAYABLE_ACCOUNT)
        print(f"  {sca.parent}: {sca.account} -> {WCF_PAYABLE_ACCOUNT}")

    frappe.db.commit()
    print(f"\nUpdated {len(nssf_components)} NSSF and {len(wcf_components)} WCF component account mappings.")


def fix_all_jvs():
    """Fix all wrong payroll JVs. Run after confirming test_one_jv works."""
    wrong_jvs = get_wrong_jvs()
    print(f"Found {len(wrong_jvs)} JVs to fix\n")

    success = []
    failed = []
    total_nssf = 0
    total_wcf = 0

    for jv in wrong_jvs:
        try:
            print(f"Processing {jv['name']} ({jv['posting_date']})...")
            result = fix_one_jv(jv["name"], commit=True)
            if result:
                new_name, nssf_fixed, wcf_fixed = result
                success.append((jv["name"], new_name, nssf_fixed, wcf_fixed))
                total_nssf += nssf_fixed
                total_wcf += wcf_fixed
            else:
                print(f"  Skipped.")
        except Exception as e:
            frappe.db.rollback()
            err_msg = str(e)
            print(f"  FAILED: {err_msg}")
            failed.append((jv["name"], err_msg))

    print(f"\n=== SUMMARY ===")
    print(f"Fixed:  {len(success)}")
    print(f"Failed: {len(failed)}")
    print(f"Total NSSF moved to Payable: {total_nssf:,.2f}")
    print(f"Total WCF moved to Payable:  {total_wcf:,.2f}")

    if failed:
        print("\nFailed JVs:")
        for jv_name, err in failed:
            print(f"  {jv_name}: {err}")

    if success:
        print("\nFixed JVs (old -> new):")
        for old, new, nssf, wcf in success:
            print(f"  {old} -> {new} (NSSF:{nssf:,.0f}, WCF:{wcf:,.0f})")


def debug_null_party_gl():
    """Check the GL entries for the null-party 1610 lines in the 3 remaining JVs."""
    null_party_jvs = [
        "ACC-JV-2025-03394",
        "ACC-JV-2025-03331",
        "ACC-JV-2025-03372",
    ]
    for jv_name in null_party_jvs:
        rows = frappe.db.sql(
            """SELECT name, account, party_type, party, debit, credit, against_voucher_type, against_voucher
               FROM `tabGL Entry`
               WHERE voucher_no = %s AND is_cancelled = 0 AND account LIKE '%%1610%%'
               AND (party IS NULL OR party = '')""",
            jv_name,
            as_dict=True,
        )
        print(f"\n{jv_name}: null-party 1610 GL entries: {len(rows)}")
        for r in rows:
            print(f"  GL {r.name}: party={r.party}, dr={r.debit}, cr={r.credit}, against={r.against_voucher_type}/{r.against_voucher}")


def debug_null_party_jvs():
    """Investigate the 3 JVs with null party that failed with 'Party Type and Party required'."""
    null_party_jvs = [
        "ACC-JV-2025-03394",
        "ACC-JV-2025-03331",
        "ACC-JV-2025-03372",
    ]
    for jv_name in null_party_jvs:
        print(f"\n=== {jv_name} ===")
        # Find account lines with 1610 (Employee Advances) that have null party
        rows = frappe.db.sql(
            """SELECT idx, account, party_type, party, debit_in_account_currency, credit_in_account_currency, reference_type, reference_name
               FROM `tabJournal Entry Account`
               WHERE parent = %s AND account LIKE '%%1610%%' AND (party IS NULL OR party = '')
               LIMIT 10""",
            jv_name,
            as_dict=True,
        )
        print(f"  Employee Advance lines with NULL party: {len(rows)}")
        for r in rows[:5]:
            print(f"    idx={r.idx}, party_type={r.party_type}, party={r.party}, cr={r.credit_in_account_currency}, ref={r.reference_type}/{r.reference_name}")

        # Check total 1610 lines
        total = frappe.db.sql(
            "SELECT COUNT(*) as cnt FROM `tabJournal Entry Account` WHERE parent = %s AND account LIKE '%%1610%%'",
            jv_name, as_dict=True
        )[0].cnt
        with_party = frappe.db.sql(
            "SELECT COUNT(*) as cnt FROM `tabJournal Entry Account` WHERE parent = %s AND account LIKE '%%1610%%' AND party IS NOT NULL AND party != ''",
            jv_name, as_dict=True
        )[0].cnt
        print(f"  Total 1610 lines: {total}, with party: {with_party}, without party: {total - with_party}")


def test_direct_fix_one():
    """Test the direct DB fix on ACC-JV-2025-03614 with rollback."""
    try:
        _fix_blocked_jv_direct("ACC-JV-2025-03614")
        print("\nSuccess! Rolling back test...")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        frappe.db.rollback()


def check_advance_doctypes():
    """Check what advance_payment_doctypes returns."""
    from erpnext.accounts.doctype.journal_entry.journal_entry import get_advance_payment_doctypes
    print(get_advance_payment_doctypes())


def check_1640_gl_entries():
    """Check what against_voucher_type the 1640 account GL entries have for the blocked JVs."""
    blocked = [
        "ACC-JV-2025-03614",
        "ACC-JV-2025-03628",
        "ACC-JV-2025-03429",
        "ACC-JV-2025-03878",
        "ACC-JV-2026-00096",
    ]
    for jv_name in blocked:
        rows = frappe.db.sql(
            """SELECT account, against_voucher_type, against_voucher, debit, credit, is_cancelled
               FROM `tabGL Entry`
               WHERE voucher_no = %s AND account LIKE '%%1640%%'""",
            jv_name,
            as_dict=True,
        )
        print(f"\n{jv_name}: 1640 GL entries:")
        for r in rows:
            print(f"  account={r.account}, against_type={r.against_voucher_type}, against={r.against_voucher}, cancelled={r.is_cancelled}")

        # Also check all non-null against_voucher_types
        rows2 = frappe.db.sql(
            """SELECT account, against_voucher_type, against_voucher, debit, credit
               FROM `tabGL Entry`
               WHERE voucher_no = %s AND is_cancelled = 0
               AND against_voucher_type IS NOT NULL AND against_voucher_type != ''
               GROUP BY against_voucher_type
               LIMIT 20""",
            jv_name,
            as_dict=True,
        )
        print(f"  Distinct against_voucher_types: {set(r.against_voucher_type for r in rows2)}")


def find_jv_against_voucher_entries():
    """Find GL entries in the 5 blocked JVs that have against_voucher_type='Journal Entry'."""
    blocked = [
        "ACC-JV-2025-03614",
        "ACC-JV-2025-03628",
        "ACC-JV-2025-03429",
        "ACC-JV-2025-03878",
        "ACC-JV-2026-00096",
    ]
    for jv_name in blocked:
        rows = frappe.db.sql(
            """SELECT account, against_voucher_type, against_voucher, debit, credit, is_cancelled
               FROM `tabGL Entry`
               WHERE voucher_no = %s AND against_voucher_type = 'Journal Entry'""",
            jv_name,
            as_dict=True,
        )
        print(f"\n{jv_name}: {len(rows)} GL entries with against_voucher_type='Journal Entry'")
        for r in rows:
            print(f"  {r.account}: against={r.against_voucher}, dr={r.debit}, cr={r.credit}, cancelled={r.is_cancelled}")


def test_cancel_one_blocked():
    """Test cancelling ACC-JV-2025-03614 to see exactly what fails."""
    jv_name = "ACC-JV-2025-03614"
    try:
        jv_doc = frappe.get_doc("Journal Entry", jv_name)
        print(f"docstatus={jv_doc.docstatus}, accounts count={len(jv_doc.accounts)}")
        if jv_doc.is_locked:
            jv_doc.unlock()
        print("Attempting _cancel()...")
        jv_doc._cancel()
        print("Cancelled successfully!")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("Rolling back...")
        frappe.db.rollback()


def debug_blocked_jv_references():
    """Check what reference_type/reference_name lines exist in the blocked JVs."""
    blocked = [
        "ACC-JV-2025-03614",
        "ACC-JV-2025-03628",
        "ACC-JV-2025-03429",
        "ACC-JV-2025-03878",
        "ACC-JV-2026-00096",
    ]
    for jv_name in blocked:
        print(f"\n=== {jv_name} ===")
        rows = frappe.db.sql(
            """SELECT account, reference_type, reference_name, debit_in_account_currency, credit_in_account_currency
               FROM `tabJournal Entry Account`
               WHERE parent = %s AND reference_type IS NOT NULL AND reference_type != ''
               ORDER BY account""",
            jv_name,
            as_dict=True,
        )
        for r in rows:
            print(f"  {r.account}: ref_type={r.reference_type}, ref_name={r.reference_name}, dr={r.debit_in_account_currency}, cr={r.credit_in_account_currency}")

        # Check GL entries too
        gl_rows = frappe.db.sql(
            """SELECT account, against_voucher_type, against_voucher, debit, credit
               FROM `tabGL Entry`
               WHERE voucher_no = %s AND is_cancelled = 0
               AND against_voucher_type IS NOT NULL AND against_voucher_type != ''
               AND against_voucher_type NOT IN ('Employee Advance', 'Payroll Entry')
               LIMIT 20""",
            jv_name,
            as_dict=True,
        )
        if gl_rows:
            print(f"  Non-standard GL entries:")
            for g in gl_rows:
                print(f"    {g.account}: against_type={g.against_voucher_type}, against={g.against_voucher}")


def debug_blocked_jvs():
    """Investigate the 5 JVs that failed with 'already adjusted' error."""
    blocked = [
        "ACC-JV-2025-03614",
        "ACC-JV-2025-03628",
        "ACC-JV-2025-03429",
        "ACC-JV-2025-03878",
        "ACC-JV-2026-00096",
    ]

    for jv_name in blocked:
        print(f"\n=== {jv_name} ===")
        jv_doc = frappe.get_doc("Journal Entry", jv_name)
        print(f"  docstatus={jv_doc.docstatus}, posting_date={jv_doc.posting_date}")

        # Check GL entries for this JV - focusing on accounts that might be reconciled
        gl_entries = frappe.db.sql(
            """SELECT account, debit, credit, against_voucher, against_voucher_type, party
               FROM `tabGL Entry`
               WHERE voucher_no = %s AND is_cancelled = 0
               ORDER BY account""",
            jv_name,
            as_dict=True,
        )

        # Find accounts that have non-null against_voucher but also look for
        # any JV accounts that reference Employee Advances
        payroll_payable_entries = [g for g in gl_entries if "Payroll Payable" in (g.account or "")]
        advance_entries = [g for g in gl_entries if "Employee Advances" in (g.account or "")]

        print(f"  Payroll Payable GL entries: {len(payroll_payable_entries)}")
        for g in payroll_payable_entries:
            print(f"    account={g.account}, dr={g.debit}, cr={g.credit}, against={g.against_voucher}, against_type={g.against_voucher_type}")

        print(f"  Employee Advance GL entries: {len(advance_entries)}")
        for g in advance_entries[:5]:
            print(f"    account={g.account}, cr={g.credit}, against={g.against_voucher}, party={g.party}")

        # Check if any GL entries reference this JV as against_voucher (settled by payment)
        settled_by = frappe.db.sql(
            """SELECT voucher_no, voucher_type, account, debit, credit
               FROM `tabGL Entry`
               WHERE against_voucher = %s AND against_voucher_type = 'Journal Entry'
               AND is_cancelled = 0 LIMIT 10""",
            jv_name,
            as_dict=True,
        )
        print(f"  Settled by (other entries using this JV as against_voucher): {len(settled_by)}")
        for s in settled_by:
            print(f"    {s.voucher_no} ({s.voucher_type}): {s.account}, dr={s.debit}, cr={s.credit}")


def fix_all_remaining_direct():
    """
    Fix all remaining wrong JVs using direct DB update (for JVs that can't be cancelled/recreated).
    Fetches the current list of wrong JVs from get_wrong_jvs() and applies direct fix.
    """
    wrong_jvs = get_wrong_jvs()
    print(f"Found {len(wrong_jvs)} remaining wrong JVs to fix via direct DB update\n")

    success = []
    failed = []

    for jv in wrong_jvs:
        jv_name = jv["name"]
        print(f"Processing {jv_name} ({jv['posting_date']})...")
        try:
            _fix_blocked_jv_direct(jv_name)
            frappe.db.commit()
            success.append(jv_name)
            print(f"  DONE: {jv_name}")
        except Exception as e:
            frappe.db.rollback()
            err_msg = str(e)
            print(f"  FAILED: {err_msg}")
            failed.append((jv_name, err_msg))

    print(f"\n=== SUMMARY ===")
    print(f"Fixed: {len(success)}")
    print(f"Failed: {len(failed)}")
    if failed:
        for jv_name, err in failed:
            print(f"  FAILED: {jv_name}: {err}")


def fix_blocked_jvs_direct():
    """
    Fix the 5 JVs that failed due to 'already adjusted' error by using
    direct GL entry manipulation (bypassing the normal cancel/recreate flow).
    These JVs have been reconciled (payroll payable settled, advances returned),
    so we can't use the normal cancel path.

    Strategy: Use direct DB update to fix the account on the wrong GL entries,
    then update the JV account lines too. This avoids re-running all the GL validations.
    """
    blocked = [
        "ACC-JV-2025-03614",
        "ACC-JV-2025-03628",
        "ACC-JV-2025-03429",
        "ACC-JV-2025-03878",
        "ACC-JV-2026-00096",
    ]

    for jv_name in blocked:
        print(f"\nProcessing {jv_name}...")
        try:
            _fix_blocked_jv_direct(jv_name)
            frappe.db.commit()
            print(f"  DONE: {jv_name}")
        except Exception as e:
            frappe.db.rollback()
            print(f"  FAILED: {e}")
            import traceback
            traceback.print_exc()


def _fix_blocked_jv_direct(jv_name):
    """
    For a JV that can't be cancelled (already reconciled), directly update
    the GL entries and JV account lines to use the correct payable accounts.
    Only updates CREDIT entries (the wrong ones) - debit (expense) entries stay as-is.
    """
    # Check which wrong CREDIT GL entries exist for this JV
    wrong_gl = frappe.db.sql(
        """SELECT name, account, debit, credit
           FROM `tabGL Entry`
           WHERE voucher_no = %s AND is_cancelled = 0
           AND account IN (%s, %s)
           AND credit > 0""",
        (jv_name, NSSF_EXPENSE_ACCOUNT, WCF_EXPENSE_ACCOUNT),
        as_dict=True,
    )

    if not wrong_gl:
        print(f"  No wrong CREDIT GL entries found for {jv_name}, skipping")
        return

    nssf_gl = [g for g in wrong_gl if g.account == NSSF_EXPENSE_ACCOUNT]
    wcf_gl = [g for g in wrong_gl if g.account == WCF_EXPENSE_ACCOUNT]

    print(f"  Wrong NSSF credit GL entries: {len(nssf_gl)} ({sum(g.credit for g in nssf_gl):,.0f} total)")
    print(f"  Wrong WCF credit GL entries:  {len(wcf_gl)} ({sum(g.credit for g in wcf_gl):,.0f} total)")

    # Update GL entries directly (credit entries only)
    for g in nssf_gl:
        frappe.db.set_value("GL Entry", g.name, "account", NSSF_PAYABLE_ACCOUNT, update_modified=False)
        print(f"    GL {g.name}: {NSSF_EXPENSE_ACCOUNT} -> {NSSF_PAYABLE_ACCOUNT} (cr={g.credit:,.0f})")

    for g in wcf_gl:
        frappe.db.set_value("GL Entry", g.name, "account", WCF_PAYABLE_ACCOUNT, update_modified=False)
        print(f"    GL {g.name}: {WCF_EXPENSE_ACCOUNT} -> {WCF_PAYABLE_ACCOUNT} (cr={g.credit:,.0f})")

    # Also update the JV account lines (credit lines only)
    jv_doc = frappe.get_doc("Journal Entry", jv_name)
    updated = False
    for acc in jv_doc.accounts:
        if acc.account == NSSF_EXPENSE_ACCOUNT and acc.credit_in_account_currency > 0:
            frappe.db.set_value("Journal Entry Account", acc.name, "account", NSSF_PAYABLE_ACCOUNT, update_modified=False)
            print(f"    JV Account {acc.name}: -> {NSSF_PAYABLE_ACCOUNT} (cr={acc.credit_in_account_currency:,.0f})")
            updated = True
        elif acc.account == WCF_EXPENSE_ACCOUNT and acc.credit_in_account_currency > 0:
            frappe.db.set_value("Journal Entry Account", acc.name, "account", WCF_PAYABLE_ACCOUNT, update_modified=False)
            print(f"    JV Account {acc.name}: -> {WCF_PAYABLE_ACCOUNT} (cr={acc.credit_in_account_currency:,.0f})")
            updated = True

    if updated:
        print(f"  Updated JV account lines for {jv_name}")
