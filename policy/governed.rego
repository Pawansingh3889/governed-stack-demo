# Gateway authorization policy. Evaluated by OPA for every tool call, before the
# request reaches mcpo and the in-tool gates. Default deny: a role may call only
# the tools its allow-list permits, and KQL control commands need the manager role.
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

needs_manager if {
	is_control_command
	input.role != "manager"
}

allow if {
	role_allows
	not needs_manager
}

reason := "ok" if allow

reason := "role not permitted for this tool" if not role_allows

reason := "control commands require the manager role" if {
	role_allows
	needs_manager
}

decision := {"allow": allow, "reason": reason}
