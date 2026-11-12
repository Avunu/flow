# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Flow used to be open to every Desk User; it now needs the Flow User role.

Create the role and grant it to every enabled System User, so nobody loses access on migrate.
Administrators can prune it afterwards. Idempotent: a user who already holds the role is skipped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import frappe

from flow.permissions import FLOW_USER_ROLE, ensure_flow_role

if TYPE_CHECKING:
	from frappe.core.doctype.user.user import User

# A user whose roles come from role profiles loses any role outside them on save, so those users
# get this profile (Flow User and nothing else) in addition to their own; shared profiles stay as they are.
# A user holding roles their profiles do not grant gets a profile of their own for those roles too, so
# the save does not drop them.
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


def _personal_role_profile(user: str, roles: set[str]) -> str:
	name = f"{FLOW_USER_ROLE} ({user})"[:140]
	frappe.get_doc(
		{
			"doctype": "Role Profile",
			"role_profile": name,
			"roles": [{"role": role} for role in sorted({FLOW_USER_ROLE, *roles})],
		}
	).insert(ignore_permissions=True)
	return name


def _grant(user: User) -> None:
	if not user.role_profiles:
		user.add_roles(FLOW_USER_ROLE)
		return
	granted = set(
		frappe.get_all(
			"Has Role",
			filters={
				"parent": ("in", [row.role_profile for row in user.role_profiles]),
				"parenttype": "Role Profile",
			},
			pluck="role",
		)
	)
	outside = {str(row.role) for row in user.roles} - granted
	profile = _personal_role_profile(str(user.name), outside) if outside else FLOW_USER_ROLE_PROFILE
	user.append("role_profiles", {"role_profile": profile})
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
		# one user's bad data must not stop the migrate; the rest of their grant is rolled back
		user = cast("User", frappe.get_doc("User", name))
		if user.role_profiles:
			_ensure_role_profile()  # a clashing profile is the admin's to fix, so it stops the patch
		frappe.db.savepoint("grant_flow_role")
		try:
			_grant(user)
		except frappe.ValidationError:
			frappe.db.rollback(save_point="grant_flow_role")
			frappe.log_error(title=f"Flow User role not granted to {name}")
