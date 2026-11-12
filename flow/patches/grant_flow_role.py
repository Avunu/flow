# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Flow used to be open to every Desk User; it now needs the Flow User role.

Create the role and grant it to every enabled System User, so nobody loses access on migrate.
Administrators can prune it afterwards. Idempotent: a user who already holds the role is skipped.
"""

import frappe

from flow.permissions import FLOW_USER_ROLE, ensure_flow_role

# A user whose roles come from role profiles loses any role outside them on save, so those users
# get this profile (Flow User and nothing else) in addition to their own; shared profiles stay as they are.
FLOW_USER_ROLE_PROFILE = FLOW_USER_ROLE


def _ensure_role_profile() -> None:
	if not frappe.db.exists("Role Profile", FLOW_USER_ROLE_PROFILE):
		frappe.get_doc(
			{
				"doctype": "Role Profile",
				"role_profile": FLOW_USER_ROLE_PROFILE,
				"roles": [{"role": FLOW_USER_ROLE}],
			}
		).insert(ignore_permissions=True)
		return
	roles = set(
		frappe.get_all(
			"Has Role", filters={"parent": FLOW_USER_ROLE_PROFILE, "parenttype": "Role Profile"}, pluck="role"
		)
	)
	if roles != {FLOW_USER_ROLE}:
		frappe.throw(
			f"Role Profile {FLOW_USER_ROLE_PROFILE!r} already exists with other roles; rename it before migrating Flow."
		)


def _grant(user) -> None:
	if not user.role_profiles:
		user.add_roles(FLOW_USER_ROLE)
		return
	_ensure_role_profile()
	user.append("role_profiles", {"role_profile": FLOW_USER_ROLE_PROFILE})
	user.save(ignore_permissions=True)


def execute():
	ensure_flow_role()
	holders = set(
		frappe.get_all("Has Role", filters={"role": FLOW_USER_ROLE, "parenttype": "User"}, pluck="parent")
	)
	users = frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User", "name": ("not in", ("Administrator", *holders))},
		pluck="name",
	)
	for name in users:
		_grant(frappe.get_doc("User", name))
