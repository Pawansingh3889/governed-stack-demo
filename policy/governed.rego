# Gateway authorization policy. Evaluated by OPA for every tool call, before the
# request reaches mcpo and the in-tool gates. Default deny: a role may call only
# the tools its allow-list permits, and KQL control commands need an operator
# role (manager or administrator).
package governed

default allow := false

action := sprintf("%s/%s", [input.server, input.tool])

patterns := object.get(data.roles, [input.role, "tools"], [])

match(p) if p == "*"
match(p) if p == action
match(p) if p == sprintf("%s/*", [input.server])

role_allows if {
	some p in patterns
	match(p)
}

# A KQL control command is a query whose first non-space character is a dot.
query := object.get(input, ["args", "query"], "")

is_control_command if {
	input.server == "kql-sop"
	input.tool == "run_kql"
	startswith(trim_space(query), ".")
}

# Control commands are for operators of the system, not ordinary data roles.
control_roles := {"manager", "administrator"}

needs_manager if {
	is_control_command
	not control_roles[input.role]
}

# Per-role data budget. The gateway passes the role's cumulative response bytes
# as input.spent; a budget_bytes of 0 (or unset) means unlimited.
budget := object.get(data.roles, [input.role, "budget_bytes"], 0)

over_budget if {
	budget > 0
	object.get(input, ["spent"], 0) >= budget
}

allow if {
	role_allows
	not needs_manager
	not over_budget
}

reason := "ok" if allow

reason := "role not permitted for this tool" if not role_allows

reason := "control commands require the manager role" if {
	role_allows
	needs_manager
}

reason := "data budget exhausted for this role" if {
	role_allows
	not needs_manager
	over_budget
}

decision := {"allow": allow, "reason": reason}
